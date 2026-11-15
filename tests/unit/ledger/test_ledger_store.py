"""Persistence shapes: inserts for orders/trades/audit, upsert for positions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import create_mock_engine
from sqlalchemy.engine import Dialect
from sqlalchemy.engine.mock import MockConnection
from sqlalchemy.orm import Session

from aegis_core import ApprovedOrder, Order, OrderIntent, OrderType, Position, Side, Trade
from aegis_ledger import AuditEvent, EventType, LedgerStore, build_position_upsert

AT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def postgres_dialect() -> Dialect:
    engine: MockConnection = create_mock_engine(
        "postgresql+psycopg://", lambda *args, **kwargs: None
    )
    return engine.dialect


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> object:
        self.statements.append(statement)
        return object()


def approved() -> ApprovedOrder:
    intent = OrderIntent(
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("0.5"),
        price=Decimal("30000"),
        stop_loss=Decimal("29000"),
        strategy_id="simple_ema_cross",
        reason="test",
        created_at=AT,
    )
    return ApprovedOrder(
        intent=intent, approval_id="appr-1", max_risk_amount=Decimal("100"), approved_at=AT
    )


def test_position_upsert_updates_current_state_on_conflict() -> None:
    statement = build_position_upsert(
        Position(symbol="BTC/USDT", qty=Decimal("0.5"), avg_entry=Decimal("30000"))
    )
    sql = str(statement.compile(dialect=postgres_dialect()))

    assert "INSERT INTO trading.positions" in sql
    assert "ON CONFLICT (symbol) DO UPDATE SET" in sql
    for column in ("qty", "avg_entry", "unrealized_pnl", "realized_pnl", "opened_at"):
        assert f"{column} =" in sql


def test_store_writes_each_row_type() -> None:
    session = RecordingSession()
    store = LedgerStore(cast(Session, session))
    approval = approved()
    order = Order.from_approved(approval, broker="paper").mark_submitted("paper-1")
    trade = Trade(
        order_id=order.order_id,
        symbol="BTC/USDT",
        side=Side.BUY,
        qty=Decimal("0.5"),
        price=Decimal("30000"),
        fee=Decimal("7.5"),
        filled_at=AT,
        strategy_id="simple_ema_cross",
    )

    store.save_order(order, approval)
    store.save_trade(trade)
    store.save_position(Position(symbol="BTC/USDT", qty=Decimal("0.5"), avg_entry=Decimal("30000")))
    store.append_audit(
        AuditEvent(event_type=EventType.ORDER_SUBMITTED, entity_type="order", entity_id="ord-1")
    )

    rendered = [
        str(statement.compile(dialect=postgres_dialect())) for statement in session.statements
    ]
    assert "INSERT INTO trading.orders" in rendered[0]
    assert "INSERT INTO trading.trades" in rendered[1]
    assert "INSERT INTO trading.positions" in rendered[2]
    assert "INSERT INTO audit.audit_logs" in rendered[3]


def test_in_memory_journal_only_appends() -> None:
    from aegis_ledger import EventType, InMemoryJournal

    journal = InMemoryJournal()
    journal.append(AuditEvent(event_type=EventType.ORDER_APPROVED, entity_type="order"))
    journal.append(AuditEvent(event_type=EventType.TRADE_RECORDED, entity_type="trade"))

    assert journal.event_types() == (EventType.ORDER_APPROVED, EventType.TRADE_RECORDED)
    assert len(journal.events) == 2
