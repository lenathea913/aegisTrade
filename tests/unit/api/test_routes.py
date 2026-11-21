"""HTTP surface: status codes, payload shapes, metrics and error mapping."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from aegis_config import Settings, load_settings
from aegis_core import Side, Trade
from aegis_ledger import AuditEvent, EventType, PortfolioLedger
from aegis_observability import Metrics
from api.app import create_app
from api.service import ApiService

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FakeRepository:
    def __init__(
        self,
        *,
        trades: Sequence[Trade] = (),
        events: Sequence[AuditEvent] = (),
        database_ok: bool = True,
    ) -> None:
        self._trades = tuple(trades)
        self._events = tuple(events)
        self.database_ok = database_ok

    def ping(self) -> bool:
        return self.database_ok

    def reconstruct(self, ledger: PortfolioLedger, *, strict: bool = True) -> None:
        """Offline fake: nothing is stored, so the book keeps what the test set."""

    def trades(self, limit: int) -> Sequence[Trade]:
        return self._trades[:limit]

    def audit_events(self, limit: int) -> Sequence[AuditEvent]:
        return self._events[:limit]


class BrokenRepository(FakeRepository):
    def trades(self, limit: int) -> Sequence[Trade]:
        raise SQLAlchemyError("database is down")


def trade(at: datetime = NOW, qty: str = "0.02") -> Trade:
    return Trade(
        order_id="ord-1",
        symbol="BTC/USDT",
        side=Side.BUY,
        qty=Decimal(qty),
        price=Decimal("30000"),
        fee=Decimal("0.3"),
        filled_at=at,
        strategy_id="simple_ema_cross",
    )


def build_client(
    *,
    repository: FakeRepository | None = None,
    ledger: PortfolioLedger | None = None,
    settings: Settings | None = None,
    metrics: Metrics | None = None,
) -> tuple[TestClient, Metrics]:
    settings = settings or load_settings("paper")
    service = ApiService(
        settings=settings,
        repository=repository or FakeRepository(),
        ledger=ledger or PortfolioLedger(initial_equity=Decimal("10000")),
        clock=lambda: NOW,
    )
    metrics = metrics or Metrics()
    app = create_app(settings=settings, service=service, metrics=metrics)
    return TestClient(app), metrics


def test_health_endpoint() -> None:
    client, _ = build_client()

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["environment"] == "paper"
    assert body["execution_mode"] == "paper"
    assert body["kill_switch"] is False


def test_positions_endpoint_serialises_money_as_strings() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(trade())
    ledger.mark_price("BTC/USDT", Decimal("31000"))
    client, _ = build_client(ledger=ledger)

    response = client.get("/positions")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert Decimal(body["positions"][0]["qty"]) == Decimal("0.02")
    assert Decimal(body["positions"][0]["unrealized_pnl"]) == Decimal("20")


def test_positions_route_reports_naked_positions() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(trade())
    client, metrics = build_client(ledger=ledger)

    client.get("/positions")

    # A position with no recorded venue stop id is exposure the venue is not covering.
    assert metrics.value("aegis_positions_naked") == 1


def test_risk_budget_endpoint() -> None:
    client, metrics = build_client()

    response = client.get("/risk/budget")

    assert response.status_code == 200
    body = response.json()
    assert body["equity"] == "10000"
    assert body["limits"]["max_leverage"] == "1"
    assert body["limits"]["allowed_symbols"] == [
        "1000PEPE/USDT",
        "BNB/USDT",
        "BTC/USDT",
        "DOGE/USDT",
        "ETH/USDT",
        "HYPE/USDT",
        "NEAR/USDT",
        "SOL/USDT",
        "XRP/USDT",
        "ZEC/USDT",
    ]
    assert body["limits"]["max_open_positions"] == 3
    # The equity gauge is updated for Prometheus scrapes.
    assert metrics.value("aegis_ledger_equity") == 10000.0


def test_journal_endpoint_merges_both_sources() -> None:
    repository = FakeRepository(
        trades=[trade(at=NOW - timedelta(minutes=1))],
        events=[AuditEvent(event_type=EventType.ORDER_APPROVED, created_at=NOW)],
    )
    client, _ = build_client(repository=repository)

    response = client.get("/journal", params={"limit": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["limit"] == 5
    assert body["entries"][0]["kind"] == "audit"
    assert body["entries"][1]["kind"] == "trade"


def test_journal_reports_a_database_outage_as_503() -> None:
    client, _ = build_client(repository=BrokenRepository())

    response = client.get("/journal")

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]


def test_journal_validates_the_limit() -> None:
    client, _ = build_client()

    assert client.get("/journal", params={"limit": 0}).status_code == 422
    assert client.get("/journal", params={"limit": 501}).status_code == 422


def test_depleted_ledger_maps_to_409() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(trade(qty="1"))
    ledger.mark_price("BTC/USDT", Decimal("1"))
    client, _ = build_client(ledger=ledger)

    response = client.get("/positions")

    assert response.status_code == 409
    assert "equity depleted" in response.json()["detail"]


def test_metrics_endpoint_exposes_prometheus_text() -> None:
    client, metrics = build_client()
    metrics.record_decision(False, ("max_leverage_exceeded:2>1",))

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "aegis_risk_decisions_total" in response.text
    assert 'reason="max_leverage_exceeded"' in response.text


def test_metrics_endpoint_can_be_disabled() -> None:
    settings = load_settings("paper")
    disabled = settings.model_copy(
        update={
            "observability": settings.observability.model_copy(update={"metrics_enabled": False})
        }
    )
    client, _ = build_client(settings=disabled)

    assert client.get("/metrics").status_code == 404


def test_requests_are_counted_by_route() -> None:
    client, metrics = build_client()

    client.get("/health")

    assert (
        metrics.value("aegis_api_requests_total", method="GET", path="/health", status="200") == 1
    )
