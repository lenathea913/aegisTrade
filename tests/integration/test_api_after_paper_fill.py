"""Integration: the API reports exactly what the paper pipeline just did.

No mocks of the money path: the real risk gate, paper broker, gateway, ledger and
pipeline execute the trade, and the API rebuilds its book from the stored trades plus
the audit trail the pipeline produced. Only PostgreSQL is replaced by an in-memory
repository, which replays trades the same way the SQL one does.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from aegis_config import load_settings
from aegis_core import (
    ApprovedOrder,
    MarketContext,
    MarketQuote,
    Order,
    OrderIntent,
    OrderType,
    Position,
    Side,
    Trade,
    utc_now,
)
from aegis_execution import ExecutionGateway, TradingPipeline, build_broker
from aegis_ledger import AuditEvent, PortfolioLedger
from aegis_observability import Metrics
from aegis_risk import RiskGate
from api.app import create_app
from api.service import ApiService

EQUITY = Decimal("10000")


class RecordingWriter:
    def __init__(self) -> None:
        self.orders: list[tuple[Order, ApprovedOrder]] = []
        self.trades: list[Trade] = []
        self.positions: list[Position] = []
        self.audits: list[AuditEvent] = []

    def save_order(self, order: Order, approved: ApprovedOrder) -> None:
        self.orders.append((order, approved))

    def save_trade(self, trade: Trade) -> None:
        self.trades.append(trade)

    def save_position(self, position: Position) -> None:
        self.positions.append(position)

    def append_audit(self, event: AuditEvent) -> None:
        self.audits.append(event)


class FakeRepository:
    """Stands in for ``trading.trades`` / ``audit.audit_logs``."""

    def __init__(self, trades: Sequence[Trade], events: Sequence[AuditEvent]) -> None:
        self._trades = tuple(trades)
        self._events = tuple(events)

    def ping(self) -> bool:
        return True

    def reconstruct(self, ledger: PortfolioLedger, *, strict: bool = True) -> None:
        """Replay the stored trades, exactly as the PostgreSQL implementation does."""
        ledger.reset()
        for stored in self._trades:
            ledger.apply_trade(stored)

    def trades(self, limit: int) -> Sequence[Trade]:
        return self._trades[:limit]

    def audit_events(self, limit: int) -> Sequence[AuditEvent]:
        return self._events[:limit]


def intent_at(now: datetime, **overrides: Any) -> OrderIntent:
    fields: dict[str, Any] = {
        "symbol": "BTC/USDT",
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "qty": Decimal("0.02"),
        "price": Decimal("30000"),
        "stop_loss": Decimal("29000"),
        "take_profit": Decimal("32000"),
        "strategy_id": "simple_ema_cross",
        "reason": "ema(12) crossed above ema(26)",
        "created_at": now - timedelta(seconds=2),
    }
    return OrderIntent(**{**fields, **overrides})


def market_at(now: datetime) -> MarketContext:
    return MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT", price=Decimal("30000"), as_of=now - timedelta(seconds=1)
            ),
        ),
        now=now,
    )


def test_api_reports_the_trade_the_pipeline_just_made() -> None:
    settings = load_settings("paper")
    ledger = PortfolioLedger(initial_equity=EQUITY)
    writer = RecordingWriter()
    pipeline = TradingPipeline(
        gate=RiskGate.from_settings(settings),
        gateway=ExecutionGateway(
            build_broker(settings.execution),
            allow_live_orders=settings.execution.allow_live_orders,
        ),
        ledger=ledger,
        writer=writer,
    )
    now = utc_now()
    result = pipeline.process(intent_at(now), market_at(now))
    assert result.filled is True
    assert result.trade is not None

    service = ApiService(
        settings=settings,
        repository=FakeRepository(writer.trades, writer.audits),
        ledger=ledger,
        clock=lambda: now,
    )
    client = TestClient(create_app(settings=settings, service=service, metrics=Metrics()))

    positions = client.get("/positions").json()
    assert positions["count"] == 1
    assert positions["positions"][0]["qty"] == "0.02"
    assert positions["positions"][0]["avg_entry"] == "30000"

    budget = client.get("/risk/budget").json()
    assert Decimal(budget["equity"]) == ledger.equity
    assert Decimal(budget["equity"]) == EQUITY - result.trade.fee
    assert budget["open_positions"] == 1
    assert Decimal(budget["total_exposure"]) == Decimal("600")

    journal = client.get("/journal").json()
    assert journal["count"] == 5  # one trade + four audit events
    kinds = [entry["kind"] for entry in journal["entries"]]
    assert kinds.count("audit") == 4
    assert kinds.count("trade") == 1
    assert journal["entries"][0]["at"] >= journal["entries"][-1]["at"]

    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["execution_mode"] == "paper"
