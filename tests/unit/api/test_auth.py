"""Token authentication: exemptions, 401s and the UI's cookie path."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from pydantic import SecretStr

from aegis_config import Settings, load_settings
from aegis_core import Candle, RiskLimit, Timeframe, Trade
from aegis_ledger import AuditEvent, PortfolioLedger
from aegis_risk import RiskGate
from api.app import create_app
from api.security import COOKIE_NAME
from api.service import ApiService
from signal_service import SignalService

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
TOKEN = "dummy-token-123"


class EmptyRepository:
    def ping(self) -> bool:
        return True

    def trades(self, limit: int) -> Sequence[Trade]:
        return []

    def audit_events(self, limit: int) -> Sequence[AuditEvent]:
        return []


class EmptyCandles:
    def recent(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[Candle]:
        return []


def _limits() -> RiskLimit:
    return RiskLimit(
        max_risk_per_trade_pct=Decimal("2"),
        max_daily_loss_pct=Decimal("5"),
        max_weekly_loss_pct=Decimal("10"),
        max_exposure_per_symbol_pct=Decimal("50"),
        max_total_exposure_pct=Decimal("100"),
        max_leverage=Decimal("1"),
        allowed_symbols=frozenset({"BTC/USDT"}),
    )


def settings_with_token(token: str | None) -> Settings:
    """Paper settings whose API token is (or is not) required."""
    base = load_settings("paper")
    return base.model_copy(update={"api_token": None if token is None else SecretStr(token)})


def build_client(token: str | None = TOKEN) -> TestClient:
    settings = settings_with_token(token)
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    service = ApiService(
        settings=settings,
        repository=EmptyRepository(),
        ledger=ledger,
        clock=lambda: NOW,
    )
    signals = SignalService(
        gate=RiskGate(limits=_limits()),
        ledger=ledger,
        candles=EmptyCandles(),
        symbols=("BTC/USDT",),
        timeframe=Timeframe.H1,
        clock=lambda: NOW,
    )
    return TestClient(create_app(settings=settings, service=service, signals=signals))


def _basic(token: str) -> dict[str, str]:
    encoded = base64.b64encode(f"operator:{token}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def test_health_and_metrics_stay_open() -> None:
    client = build_client()

    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_api_requires_the_bearer_token() -> None:
    client = build_client()

    anonymous = client.get("/api/signals")

    assert anonymous.status_code == 401
    assert anonymous.json()["detail"]
    assert anonymous.headers["www-authenticate"].startswith("Basic")

    authorised = client.get("/api/signals", headers={"Authorization": f"Bearer {TOKEN}"})
    assert authorised.status_code == 200
    assert authorised.json()["count"] == 0


def test_mutations_are_never_exempt() -> None:
    client = build_client()

    assert client.post("/api/signals/generate").status_code == 401
    assert client.post("/api/signals/sig_x/convert").status_code == 401
    assert client.post("/api/signals/sig_x/ignore").status_code == 401
    assert client.post("/ui/signals/generate").status_code == 401


def test_dashboard_and_partials_require_a_token() -> None:
    client = build_client()

    html = {"accept": "text/html"}
    page = client.get("/", headers=html)
    partial = client.get("/partials/overview", headers=html)

    assert page.status_code == 401
    assert "401 Unauthorized" in page.text
    assert partial.status_code == 401


def test_a_wrong_token_is_rejected() -> None:
    client = build_client()

    response = client.get("/api/signals", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401


def test_basic_auth_logs_in_and_sets_the_htmx_cookie() -> None:
    client = build_client()

    response = client.get("/", headers={**_basic(TOKEN), "accept": "text/html"})

    assert response.status_code == 200
    assert COOKIE_NAME in response.cookies
    # The cookie alone is enough for HTMX requests afterwards.
    assert client.get("/api/signals").status_code == 200


def test_an_empty_token_means_development_mode() -> None:
    client = build_client(token=None)

    assert client.get("/api/signals").status_code == 200
    assert client.get("/", headers={"accept": "text/html"}).status_code == 200
