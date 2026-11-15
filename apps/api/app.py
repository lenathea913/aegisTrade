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
from aegis_ledger import LedgerError, PortfolioLedger
from aegis_observability import CONTENT_TYPE_LATEST, Metrics, configure_logging, get_logger
from aegis_risk import RiskGate
from api.repository import SqlTradingRepository
from api.schemas import (
    ConversionResponse,
    HealthResponse,
    IntentResponse,
    MotivationResponse,
    RiskBudgetResponse,
    SignalListResponse,
    SignalResponse,
)
from api.service import ApiService
from signal_service import (
    SignalNotFoundError,
    SignalRiskRejectedError,
    SignalService,
    SignalStateError,
    SqlCandleRepository,
)

__all__ = ["create_app"]

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

    engine = None
    session_factory = None
    if service is None:
        engine = create_engine_from_settings(settings.database)
        session_factory = create_session_factory(engine)
        service = ApiService(
            settings=settings,
            repository=SqlTradingRepository(session_factory),
            ledger=PortfolioLedger(initial_equity=settings.risk.initial_equity),
        )

    if signals is None and session_factory is not None:
        signals = SignalService(
            gate=RiskGate.from_settings(settings),
            ledger=service.ledger,
            candles=SqlCandleRepository(session_factory),
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
        if session_factory is not None:
            _restore_ledger(service, session_factory, logger)
        try:
            yield
        finally:
            if engine is not None:
                engine.dispose()
            logger.info("api_stopped")

    app = FastAPI(title="AegisTrade API", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.metrics = metrics
    app.state.signals = signals

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
            return TEMPLATES.TemplateResponse(request, "positions.html", {"positions": response})
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
            return TEMPLATES.TemplateResponse(request, "journal.html", {"journal": response})
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
        return TEMPLATES.TemplateResponse(
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
        return TEMPLATES.TemplateResponse(
            request,
            "signals.html",
            {
                "signals": items,
                "selected": selected,
                "preview": service_.risk_preview(selected) if selected else None,
            },
        )

    # HTMX partials: these are what the 5-second polls swap in.

    @app.get("/partials/motivation", include_in_schema=False)
    def motivation_partial(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request, "partials/motivation.html", {"quote": get_random_motivation()}
        )

    @app.get("/partials/overview", include_in_schema=False)
    def overview_partial(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request,
            "partials/overview.html",
            {"health": service.health(), "budget": service.risk_budget()},
        )

    @app.get("/partials/positions", include_in_schema=False)
    def positions_partial(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request, "partials/positions.html", {"positions": service.positions()}
        )

    @app.get("/partials/signals", include_in_schema=False)
    def signals_partial(request: Request) -> Response:
        service_ = _signals()
        items = service_.list_signals()
        return TEMPLATES.TemplateResponse(request, "partials/signals.html", {"signals": items})

    @app.get("/partials/signals/{signal_id}", include_in_schema=False)
    def signal_detail_partial(request: Request, signal_id: str) -> Response:
        service_ = _signals()
        signal = service_.get(signal_id)
        return TEMPLATES.TemplateResponse(
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
        try:
            conversion = service_.convert(signal_id)
        except SignalRiskRejectedError as exc:
            message = "Risk engine refused this signal: " + "; ".join(exc.reasons)
        except SignalStateError as exc:
            message = str(exc)
        signal = service_.get(signal_id)
        return TEMPLATES.TemplateResponse(
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
        return TEMPLATES.TemplateResponse(
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
        return TEMPLATES.TemplateResponse(request, "partials/signals.html", {"signals": items})

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
