"""End-to-end: one ``OrderIntent`` becomes an audited, ledgered fill — or nothing.

This is the test the project exists for. It wires the real configuration, the real
risk gate, the real paper broker, the real gateway and the real ledger together,
then proves:

* an approved intent fills, updates the position and writes four audit events;
* the written order row still carries the approval evidence;
* an intent that is not an ``ApprovedOrder`` cannot reach the gateway;
* with the kill switch on, nothing is ordered, filled or persisted.

Only PostgreSQL persistence is stubbed (the machine has no database); the row
payloads themselves are the real mapping functions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

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
    UnapprovedOrderError,
    utc_now,
)
from aegis_execution import ExecutionGateway, TradingPipeline, build_broker
from aegis_ledger import (
    AuditEvent,
    EventType,
    LedgerWriter,
    PortfolioLedger,
    order_row,
    position_row,
    trade_row,
)
from aegis_risk import RiskGate

EQUITY = Decimal("10000")


class RecordingWriter:
    """Records what would have been written to ``trading.*`` and ``audit.*``."""

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


def market_at(now: datetime, *, price: str = "30000") -> MarketContext:
    return MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT",
                price=Decimal(price),
                as_of=now - timedelta(seconds=1),
            ),
        ),
        now=now,
    )


def build_pipeline(
    *, kill_switch: bool = False, writer: LedgerWriter | None = None
) -> TradingPipeline:
    settings = load_settings("paper")
    gate = RiskGate(
        limits=settings.risk.limits,
        kill_switch=kill_switch,
        stale_after_seconds=settings.market_data.stale_after_for(
            settings.market_data.timeframes[0]
        ),
    )
    return TradingPipeline(
        gate=gate,
        gateway=ExecutionGateway(
            build_broker(settings.execution),
            allow_live_orders=settings.execution.allow_live_orders,
        ),
        ledger=PortfolioLedger(initial_equity=EQUITY),
        writer=writer,
    )


def test_intent_becomes_an_audited_fill_in_the_ledger() -> None:
    now = utc_now()
    writer = RecordingWriter()
    pipeline = build_pipeline(writer=writer)

    result = pipeline.process(intent_at(now), market_at(now))

    assert result.filled is True
    assert result.approved is not None
    assert result.trade is not None
    assert result.update is not None

    # Paper costs came from configuration (5 bps fee/slippage on a LIMIT fill).
    assert result.trade.fee == Decimal("0.3")
    assert result.trade.price == Decimal("30000")
    assert pipeline.ledger.position("BTC/USDT").qty == Decimal("0.02")
    assert pipeline.ledger.equity == Decimal("9999.7")

    # The persisted order row keeps the approval trail.
    order, approved = writer.orders[0]
    row = order_row(order, approved)
    assert row["status"] == "FILLED"
    assert row["approval_id"] == result.approved.approval_id
    assert row["max_risk_amount"] == result.approved.max_risk_amount
    assert row["risk_checks"] == list(result.approved.risk_checks_passed)

    # Trade and position rows are the ones the ledger computed.
    assert trade_row(writer.trades[0])["fee"] == Decimal("0.3")
    assert position_row(writer.positions[0])["qty"] == Decimal("0.02")

    # The audit trail is complete and ordered.
    assert [event.event_type for event in writer.audits] == [
        EventType.ORDER_APPROVED,
        EventType.ORDER_SUBMITTED,
        EventType.TRADE_RECORDED,
        EventType.POSITION_UPDATED,
    ]

    # And the ledger is what the next decision will be judged against.
    state = pipeline.ledger.snapshot(now=now)
    assert state.position("BTC/USDT").qty == Decimal("0.02")
    assert state.equity == Decimal("9999.7")


def test_an_unapproved_intent_cannot_reach_the_gateway() -> None:
    now = utc_now()
    settings = load_settings("paper")
    gateway = ExecutionGateway(
        build_broker(settings.execution),
        allow_live_orders=settings.execution.allow_live_orders,
    )

    with pytest.raises(UnapprovedOrderError):
        gateway.execute(cast(Any, intent_at(now)), market=market_at(now))


def test_kill_switch_blocks_the_whole_path() -> None:
    now = utc_now()
    writer = RecordingWriter()
    pipeline = build_pipeline(kill_switch=True, writer=writer)

    result = pipeline.process(intent_at(now), market_at(now))

    assert result.approved is None
    assert result.execution is None
    assert result.reasons == ("kill_switch_active",)
    assert writer.orders == []
    assert writer.trades == []
    assert writer.audits[0].event_type is EventType.RISK_REJECTED
    assert pipeline.ledger.position("BTC/USDT").is_flat
    assert pipeline.ledger.equity == EQUITY
