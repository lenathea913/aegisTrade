"""Row mapping: domain objects become exactly the columns the tables expect."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from aegis_core import (
    ApprovedOrder,
    Order,
    OrderIntent,
    OrderType,
    Position,
    Side,
    Trade,
)
from aegis_ledger import AuditEvent, EventType, audit_row, order_row, position_row, trade_row

AT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def approved() -> ApprovedOrder:
    intent = OrderIntent(
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("0.5"),
        price=Decimal("30000"),
        stop_loss=Decimal("29000"),
        take_profit=Decimal("32000"),
        strategy_id="simple_ema_cross",
        reason="ema(12) crossed above ema(26)",
        created_at=AT,
    )
    return ApprovedOrder(
        intent=intent,
        approval_id="appr-1",
        risk_checks_passed=("risk_per_trade_within_limit",),
        max_risk_amount=Decimal("100"),
        approved_at=AT,
    )


def test_order_row_keeps_the_approval_evidence() -> None:
    approval = approved()
    order = Order.from_approved(approval, broker="paper")
    order = order.mark_submitted("paper-ord-1").apply_fill(Decimal("0.5"), Decimal("30000"))

    row = order_row(order, approval)

    assert row["order_id"] == order.order_id
    assert row["approval_id"] == "appr-1"
    assert row["status"] == "FILLED"
    assert row["side"] == "BUY"
    assert row["order_type"] == "LIMIT"
    assert row["stop_loss"] == Decimal("29000")
    assert row["take_profit"] == Decimal("32000")
    assert row["strategy_id"] == "simple_ema_cross"
    assert row["reason"] == "ema(12) crossed above ema(26)"
    assert row["risk_checks"] == ["risk_per_trade_within_limit"]
    assert row["max_risk_amount"] == Decimal("100")
    assert row["exchange_order_id"] == "paper-ord-1"
    assert row["filled_qty"] == Decimal("0.5")


def test_trade_row_carries_costs() -> None:
    trade = Trade(
        trade_id="trd-1",
        order_id="ord-1",
        symbol="BTC/USDT",
        side=Side.SELL,
        qty=Decimal("0.2"),
        price=Decimal("31000"),
        fee=Decimal("3.1"),
        funding=Decimal("-0.5"),
        filled_at=AT,
        strategy_id="simple_ema_cross",
        r_multiple=Decimal("1.5"),
    )

    row = trade_row(trade)

    assert row["trade_id"] == "trd-1"
    assert row["side"] == "SELL"
    assert row["fee"] == Decimal("3.1")
    assert row["funding"] == Decimal("-0.5")
    assert row["r_multiple"] == Decimal("1.5")


def test_position_row_matches_the_table_shape() -> None:
    position = Position(symbol="BTC/USDT", qty=Decimal("-0.3"), avg_entry=Decimal("31000"))

    row = position_row(position)

    assert row["symbol"] == "BTC/USDT"
    assert row["qty"] == Decimal("-0.3")
    assert row["avg_entry"] == Decimal("31000")
    assert set(row) == {
        "symbol",
        "qty",
        "avg_entry",
        "unrealized_pnl",
        "realized_pnl",
        "leverage",
        "opened_at",
        "stop_loss",
        "take_profit",
    }


def test_audit_row_uses_the_stable_event_name() -> None:
    event = AuditEvent(
        event_type=EventType.RISK_REJECTED,
        entity_type="order_intent",
        entity_id="BTC/USDT",
        payload={"reasons": ["stale_market_data:BTC/USDT"]},
        created_at=AT,
    )

    row = audit_row(event)

    assert row["event_type"] == "risk_rejected"
    assert row["entity_id"] == "BTC/USDT"
    assert row["actor"] == "system"
    assert row["payload"] == {"reasons": ["stale_market_data:BTC/USDT"]}
