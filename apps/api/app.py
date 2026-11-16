"""FastAPI application factory: JSON API + server-rendered dashboard.

Three rules shape this module:

1. **The API cannot trade.** Routes read the ledger and the journal, and the signal
   endpoints return *draft* intents after re-running the risk engine. Execution stays
   behind ``TradingPipeline``, driven by a runner, never by HTTP.
2. **The dashboard is server-rendered.** Jinja2 + HTMX + Tailwind over a CDN: no Node
   build step, no client framework, no duplicated business logic in JavaScript.
3. **Startup reconstructs the book.** The ledger is the source of truth while a process
   runs, so it is reloaded from ``trading.trades`` at startup instead of reporting a
   fresh ``initial_equity`` that ignores history.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from aegis_config import Settings, load_settings
from aegis_core import __version__, get_random_motivation
from aegis_db import create_engine_from_settings, create_session_factory, session_scope
from aegis_execution import ExchangeError, build_broker
from aegis_ledger import LedgerError, PortfolioLedger
from aegis_observability import CONTENT_TYPE_LATEST, Metrics, configure_logging, get_logger
from aegis_risk import RiskGate
from api.exchange import cancel_order, open_orders
from api.repository import SqlTradingRepository
from api.schemas import (
    CancelOrderResponse,
    ConversionResponse,
    ExchangeOrderListResponse,
    ExchangeOrderResponse,
    ExchangeStatusResponse,
    HealthResponse,
    IntentResponse,
    MotivationResponse,
    RiskBudgetResponse,
    SignalListResponse,
    SignalResponse,
)
from api.security import ApiTokenGuard, install_api_token_middleware
from api.service import ApiService
from signal_service import (
    SignalNotFoundError,
    SignalRiskRejectedError,
    SignalService,
    SignalStateError,
    SqlCandleRepository,
)

__all__ = ["create_app"]


def _probe_exchange(settings: Settings) -> tuple[bool, str]:
    """Ping the configured venue; any failure is reported as disconnected.

    Built per probe so the API never holds credentials it does not need, and a
    non-testnet broker is only built when ``live_confirm`` actually allows it.
    """
    try:
        broker = build_broker(
            settings.execution, exchange=settings.exchange, live_confirm=settings.live_confirm
        )
    except Exception as exc:
        return False, f"exchange not configured: {exc}"
    close = getattr(broker, "close", None)
    try:
        if not broker.live:
            return True, "paper broker: fills are simulated locally"
        ping = getattr(broker, "ping", None)
        if ping is None:
            return True, "connected (this broker exposes no ping)"
        try:
            ok = bool(ping())
        except Exception as exc:
            return False, f"exchange ping failed: {exc}"
        if ok:
            return True, "testnet reachable"
        return False, "testnet unreachable"
    finally:
        # The badge is polled repeatedly: a client per probe would leak sockets.
        if callable(close):
            close()


WEB_DIR = Path(__file__).parent


def _wants_html(request: Request) -> bool:
    """Browsers ask for HTML; API clients and test clients do not."""
    return "text/html" in request.headers.get("accept", "")


TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def create_app(
    *,
    settings: Settings | None = None,
    service: ApiService | None = None,
    metrics: Metrics | None = None,
    signals: SignalService | None = None,
) -> FastAPI:
    """Build the application; everything external is injectable."""
    settings = settings or load_settings()
    configure_logging(settings.observability)
    logger = get_logger("aegis.api")
    metrics = metrics or Metrics()
    guard = ApiTokenGuard(
        None if settings.api_token is None else settings.api_token.get_secret_value()
    )

    engine = None
    session_factory = None
    if service is None:
        engine = create_engine_from_settings(settings.database)
        session_factory = create_session_factory(engine)
        service = ApiService(
            settings=settings,
            repository=SqlTradingRepository(session_factory),
            ledger=PortfolioLedger(initial_equity=settings.risk.initial_equity),
            exchange_probe=lambda: _probe_exchange(settings),
        )

    if signals is None and session_factory is not None:
        signals = SignalService(
            gate=RiskGate.from_settings(settings),
            ledger=service.ledger,
            candles=SqlCandleRepository(session_factory, environment=settings.exchange.environment),
            symbols=settings.market_data.symbols,
            timeframe=settings.market_data.timeframes[0],
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "api_started",
            environment=settings.environment.value,
            execution_mode=settings.execution.mode.value,
            metrics_enabled=settings.observability.metrics_enabled,
        )
        if not guard.enabled:
            # Development convenience, never a deployed default: say it loudly.
            logger.warning(
                "api_auth_disabled",
                detail="AEGIS_API_TOKEN is empty; /api/* and the dashboard are open",
            )
        if session_factory is not None:
            _restore_ledger(service, session_factory, logger)
        try:
            yield
        finally:
            if engine is not None:
                engine.dispose()
            logger.info("api_stopped")

    app = FastAPI(title="AegisTrade API", version=__version__, lifespan=lifespan)

    templates = TEMPLATES

    def _render(request: Request, name: str, context: dict[str, Any]) -> Response:
        """Render a template with the exchange badge always in scope."""
        return templates.TemplateResponse(
            request, name, {"exchange": service.exchange_status(), **context}
        )

    app.state.settings = settings
    app.state.service = service
    app.state.metrics = metrics
    app.state.signals = signals
    app.state.token_guard = guard

    install_api_token_middleware(app, guard, logger)

    @app.middleware("http")
    async def count_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        metrics.record_request(request.method, path, response.status_code)
        return response

    @app.exception_handler(LedgerError)
    async def ledger_problem(_request: Request, exc: LedgerError) -> JSONResponse:
        # A depleted ledger is a state problem, not a server crash.
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SignalNotFoundError)
    async def signal_missing(_request: Request, exc: SignalNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"unknown signal: {exc}"})

    @app.exception_handler(SignalStateError)
    async def signal_state(_request: Request, exc: SignalStateError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SignalRiskRejectedError)
    async def signal_rejected(_request: Request, exc: SignalRiskRejectedError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "reasons": list(exc.reasons)},
        )

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    # ------------------------------------------------------------------ ops

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        """Liveness plus database reachability and the kill-switch state."""
        return service.health()

    @app.get("/motivation", response_model=MotivationResponse, tags=["ops"])
    def motivation() -> MotivationResponse:
        """One process reminder. Never a claim about returns."""
        return MotivationResponse(quote=get_random_motivation())

    @app.get("/api/exchange/status", response_model=ExchangeStatusResponse, tags=["ops"])
    def exchange_status() -> ExchangeStatusResponse:
        """Where orders go and whether that venue answers; drives the header badge."""
        return service.exchange_status()

    def _require_exchange() -> None:
        if not service.exchange_status().connected:
            raise HTTPException(
                status_code=503, detail="exchange is not connected; the order view is read-only"
            )

    @app.get("/api/exchange/orders", response_model=ExchangeOrderListResponse, tags=["exchange"])
    def exchange_orders() -> ExchangeOrderListResponse:
        """Orders the venue still has open; empty for the paper broker."""
        _require_exchange()
        try:
            views = open_orders(settings)
        except ExchangeError as exc:
            raise HTTPException(status_code=503, detail=f"exchange error: {exc}") from exc
        return ExchangeOrderListResponse(
            count=len(views),
            orders=[
                ExchangeOrderResponse(
                    order_id=view.order_id,
                    symbol=view.symbol,
                    side=view.side,
                    status=view.status,
                    price=None if view.price is None else str(view.price),
                    qty=None if view.qty is None else str(view.qty),
                    filled_qty=str(view.filled_qty),
                )
                for view in views
            ],
        )

    @app.post(
        "/api/exchange/orders/{order_id}/cancel",
        response_model=CancelOrderResponse,
        tags=["exchange"],
    )
    def cancel_exchange_order(
        order_id: str, symbol: str = Query(min_length=3, description="e.g. BTC/USDT")
    ) -> CancelOrderResponse:
        """Cancel one live order on the venue and report its resulting status."""
        _require_exchange()
        try:
            status = cancel_order(settings, order_id, symbol)
        except ExchangeError as exc:
            raise HTTPException(status_code=503, detail=f"cancel failed: {exc}") from exc
        return CancelOrderResponse(order_id=order_id, status=status)

    # ------------------------------------------------------------- portfolio

    @app.get("/positions", tags=["portfolio"])
    def positions(request: Request) -> Response:
        """Positions: HTML for a browser, JSON for an API client.

        The dashboard and the JSON API share a path; the ``Accept`` header decides.
        Every browser sends ``text/html``, every SDK and test client does not.
        """
        response = service.positions()
        metrics.set_equity(Decimal(service.risk_budget().equity))
        if _wants_html(request):
            return _render(request, "positions.html", {"positions": response})
        return JSONResponse(content=response.model_dump(mode="json"))

    @app.get("/risk/budget", response_model=RiskBudgetResponse, tags=["risk"])
    def risk_budget() -> RiskBudgetResponse:
        """Remaining room under every configured limit."""
        budget = service.risk_budget()
        metrics.set_equity(Decimal(budget.equity))
        return budget

    @app.get("/journal", tags=["journal"])
    def journal(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500, description="max entries per source"),
    ) -> Response:
        """Trades and audit events: HTML for a browser, JSON for an API client."""
        try:
            response = service.journal(limit=limit)
        except SQLAlchemyError as exc:
            raise HTTPException(
                status_code=503,
                detail="journal unavailable: database is not reachable",
            ) from exc
        if _wants_html(request):
            return _render(request, "journal.html", {"journal": response})
        return JSONResponse(content=response.model_dump(mode="json"))

    # ---------------------------------------------------------------- signals

    def _signals() -> SignalService:
        if signals is None:
            raise HTTPException(status_code=503, detail="signal service is not configured")
        return signals

    @app.get("/api/signals", response_model=SignalListResponse, tags=["signals"])
    def list_signals() -> SignalListResponse:
        items = _signals().list_signals()
        return SignalListResponse(
            count=len(items), signals=[SignalResponse.from_domain(item) for item in items]
        )

    @app.post("/api/signals/generate", response_model=SignalListResponse, tags=["signals"])
    def generate_signals() -> SignalListResponse:
        """Scan stored candles and return every signal currently known."""
        service_ = _signals()
        service_.generate()
        items = service_.list_signals()
        return SignalListResponse(
            count=len(items), signals=[SignalResponse.from_domain(item) for item in items]
        )

    @app.get("/api/signals/{signal_id}", response_model=SignalResponse, tags=["signals"])
    def get_signal(signal_id: str) -> SignalResponse:
        return SignalResponse.from_domain(_signals().get(signal_id))

    @app.post(
        "/api/signals/{signal_id}/convert", response_model=ConversionResponse, tags=["signals"]
    )
    def convert_signal(signal_id: str) -> ConversionResponse:
        """Produce a **draft** intent; the risk engine runs again here."""
        if not service.exchange_status().connected:
            # An unreachable venue must not hand out intentions it cannot place.
            raise HTTPException(
                status_code=503, detail="exchange is not connected; conversion is disabled"
            )
        conversion = _signals().convert(signal_id)
        return ConversionResponse(
            signal=SignalResponse.from_domain(conversion.signal),
            intent=IntentResponse.from_domain(conversion.intent),
            max_risk_amount=str(conversion.decision.max_risk_amount),
            checks=list(conversion.decision.reasons),
        )

    @app.post("/api/signals/{signal_id}/ignore", response_model=SignalResponse, tags=["signals"])
    def ignore_signal(signal_id: str) -> SignalResponse:
        return SignalResponse.from_domain(_signals().ignore(signal_id))

    # -------------------------------------------------------------- dashboard

    @app.get("/", include_in_schema=False)
    def overview_page(request: Request) -> Response:
        return _render(
            request,
            "overview.html",
            {
                "health": service.health(),
                "budget": service.risk_budget(),
                "motivation": get_random_motivation(),
            },
        )

    @app.get("/signals", include_in_schema=False)
    def signals_page(request: Request) -> Response:
        service_ = _signals()
        items = service_.list_signals()
        selected = items[0] if items else None
        return _render(
            request,
            "signals.html",
            {
                "signals": items,
                "selected": selected,
                "preview": service_.risk_preview(selected) if selected else None,
            },
        )

    # -------------------------------------------------------- exchange orders

    def _exchange_orders_context(message: str | None = None) -> dict[str, Any]:
        status = service.exchange_status()
        if not status.connected:
            return {
                "orders": (),
                "message": message or f"Exchange offline: {status.detail}",
            }
        try:
            views = open_orders(settings)
        except ExchangeError as exc:
            return {"orders": (), "message": f"Cannot list exchange orders: {exc}"}
        return {"orders": views, "message": message}

    @app.get("/exchange-orders", include_in_schema=False)
    def exchange_orders_page(request: Request) -> Response:
        return _render(request, "exchange_orders.html", _exchange_orders_context())

    @app.get("/partials/exchange-orders", include_in_schema=False)
    def exchange_orders_partial(request: Request) -> Response:
        return _render(request, "partials/exchange_orders.html", _exchange_orders_context())

    @app.post("/ui/exchange-orders/{order_id}/cancel", include_in_schema=False)
    def cancel_exchange_order_from_ui(
        request: Request,
        order_id: str,
        symbol: str = Query(min_length=3, description="e.g. BTC/USDT"),
    ) -> Response:
        """Cancel from the dashboard; disabled in the UI when the venue is offline."""
        message: str | None
        if not service.exchange_status().connected:
            message = "Cancel refused: the exchange is offline."
        else:
            try:
                status_value = cancel_order(settings, order_id, symbol)
                message = f"Cancel requested for {order_id} — exchange status {status_value}."
            except ExchangeError as exc:
                message = f"Cancel failed: {exc}"
        return _render(request, "partials/exchange_orders.html", _exchange_orders_context(message))

    # HTMX partials: these are what the 5-second polls swap in.

    @app.get("/partials/motivation", include_in_schema=False)
    def motivation_partial(request: Request) -> Response:
        return _render(request, "partials/motivation.html", {"quote": get_random_motivation()})

    @app.get("/partials/overview", include_in_schema=False)
    def overview_partial(request: Request) -> Response:
        return _render(
            request,
            "partials/overview.html",
            {"health": service.health(), "budget": service.risk_budget()},
        )

    @app.get("/partials/positions", include_in_schema=False)
    def positions_partial(request: Request) -> Response:
        return _render(request, "partials/positions.html", {"positions": service.positions()})

    @app.get("/partials/signals", include_in_schema=False)
    def signals_partial(request: Request) -> Response:
        service_ = _signals()
        items = service_.list_signals()
        return _render(request, "partials/signals.html", {"signals": items})

    @app.get("/partials/signals/{signal_id}", include_in_schema=False)
    def signal_detail_partial(request: Request, signal_id: str) -> Response:
        service_ = _signals()
        signal = service_.get(signal_id)
        return _render(
            request,
            "partials/signal_detail.html",
            {
                "selected": signal,
                "preview": service_.risk_preview(signal),
                "message": None,
                "conversion": None,
            },
        )

    @app.post("/ui/signals/{signal_id}/convert", include_in_schema=False)
    def convert_from_ui(request: Request, signal_id: str) -> Response:
        service_ = _signals()
        conversion = None
        message = None
        status = service.exchange_status()
        if not status.connected:
            message = f"Convert disabled: exchange is not connected ({status.detail})."
        else:
            try:
                conversion = service_.convert(signal_id)
            except SignalRiskRejectedError as exc:
                message = "Risk engine refused this signal: " + "; ".join(exc.reasons)
            except SignalStateError as exc:
                message = str(exc)
        signal = service_.get(signal_id)
        return _render(
            request,
            "partials/signal_detail.html",
            {
                "selected": signal,
                "preview": service_.risk_preview(signal),
                "message": message,
                "conversion": conversion,
            },
        )

    @app.post("/ui/signals/{signal_id}/ignore", include_in_schema=False)
    def ignore_from_ui(request: Request, signal_id: str) -> Response:
        service_ = _signals()
        message = None
        try:
            service_.ignore(signal_id)
        except SignalStateError as exc:
            message = str(exc)
        signal = service_.get(signal_id)
        return _render(
            request,
            "partials/signal_detail.html",
            {
                "selected": signal,
                "preview": service_.risk_preview(signal),
                "message": message,
                "conversion": None,
            },
        )

    @app.post("/ui/signals/generate", include_in_schema=False)
    def generate_from_ui(request: Request) -> Response:
        service_ = _signals()
        service_.generate()
        items = service_.list_signals()
        return _render(request, "partials/signals.html", {"signals": items})

    if settings.observability.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        def prometheus_metrics() -> Response:
            return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return app


def _restore_ledger(service: ApiService, factory: sessionmaker[Session], logger: Any) -> None:
    """Reload positions and realized PnL from the database (best effort, logged)."""
    try:
        with session_scope(factory) as session:
            summary = service.ledger.reconstruct_from_db(session)
    except (LedgerError, SQLAlchemyError) as exc:
        logger.warning("ledger_reconstruction_failed", error=str(exc))
        return
    logger.info(
        "ledger_reconstructed",
        trades=summary.trades_replayed,
        open_positions=summary.open_positions,
        equity=str(summary.equity),
        daily_realized_pnl=str(summary.daily_realized_pnl),
        weekly_realized_pnl=str(summary.weekly_realized_pnl),
    )
