"""FastAPI application factory.

The API is deliberately thin and read-only: routes call ``ApiService``, which reads
the ledger and the database. No route can place an order — execution stays behind
``TradingPipeline`` and is driven by the runner (FASE 9), not by HTTP.

Construction is injectable (``create_app(settings=..., service=...)``) so tests run
without PostgreSQL, while the default wiring builds an engine lazily: importing the
app never opens a connection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from aegis_config import Settings, load_settings
from aegis_core import __version__
from aegis_db import create_engine_from_settings, create_session_factory
from aegis_ledger import LedgerError, PortfolioLedger
from aegis_observability import CONTENT_TYPE_LATEST, Metrics, configure_logging, get_logger
from api.repository import SqlTradingRepository
from api.schemas import (
    HealthResponse,
    JournalResponse,
    PositionsResponse,
    RiskBudgetResponse,
)
from api.service import ApiService

__all__ = ["create_app"]


def create_app(
    *,
    settings: Settings | None = None,
    service: ApiService | None = None,
    metrics: Metrics | None = None,
) -> FastAPI:
    """Build the application; everything external is injectable."""
    settings = settings or load_settings()
    configure_logging(settings.observability)
    logger = get_logger("aegis.api")
    metrics = metrics or Metrics()

    engine = None
    if service is None:
        engine = create_engine_from_settings(settings.database)
        service = ApiService(
            settings=settings,
            repository=SqlTradingRepository(create_session_factory(engine)),
            ledger=PortfolioLedger(initial_equity=settings.risk.initial_equity),
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "api_started",
            environment=settings.environment.value,
            execution_mode=settings.execution.mode.value,
            metrics_enabled=settings.observability.metrics_enabled,
        )
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

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        """Liveness plus database reachability and the kill-switch state."""
        return service.health()

    @app.get("/positions", response_model=PositionsResponse, tags=["portfolio"])
    def positions() -> PositionsResponse:
        """Current positions as the ledger holds them, marked."""
        response = service.positions()
        metrics.set_equity(Decimal(service.risk_budget().equity))
        return response

    @app.get("/risk/budget", response_model=RiskBudgetResponse, tags=["risk"])
    def risk_budget() -> RiskBudgetResponse:
        """Remaining room under every configured limit."""
        budget = service.risk_budget()
        metrics.set_equity(Decimal(budget.equity))
        return budget

    @app.get("/journal", response_model=JournalResponse, tags=["journal"])
    def journal(
        limit: int = Query(default=50, ge=1, le=500, description="max entries per source"),
    ) -> JournalResponse:
        """Recent trades and audit events, merged newest-first."""
        try:
            return service.journal(limit=limit)
        except SQLAlchemyError as exc:
            raise HTTPException(
                status_code=503,
                detail="journal unavailable: database is not reachable",
            ) from exc

    if settings.observability.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        def prometheus_metrics() -> Response:
            return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return app
