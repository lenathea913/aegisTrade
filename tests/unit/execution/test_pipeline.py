"""The risk-first pipeline: approve -> execute -> ledger, or reject and stop."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from aegis_core import (
    ApprovedOrder,
    MarketContext,
    MarketQuote,
    Order,
    OrderIntent,
    Position,
    PositionSide,
    RiskLimit,
    Side,
    Trade,
)
from aegis_execution import (
    Broker,
    BrokerFill,
    ExecutionError,
    ExecutionGateway,
    PaperBroker,
    TradingPipeline,
)
from aegis_ledger import (
    AuditEvent,
    EventType,
    PortfolioLedger,
)
from aegis_risk import RiskGate


class SpyBroker:
    """Counts submissions so a rejection test can prove the broker was never used."""

    name = "paper"
    live = False

    def __init__(self) -> None:
        self.calls = 0

    def submit(
        self, order: Order, *, market_price: Decimal, now: datetime | None = None
    ) -> BrokerFill | None:  # pragma: no cover - must never be called
        self.calls += 1
        raise AssertionError("broker must not be called")


class RecordingWriter:
    """In-memory stand-in for ``LedgerStore``."""

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


def build(
    gate: RiskGate,
    *,
    broker: Broker | None = None,
    ledger: PortfolioLedger | None = None,
    writer: RecordingWriter | None = None,
) -> TradingPipeline:
    return TradingPipeline(
        gate=gate,
        gateway=ExecutionGateway(broker or PaperBroker()),
        ledger=ledger or PortfolioLedger(initial_equity=Decimal("10000")),
        writer=writer,
    )


def test_approved_intent_fills_and_updates_positions_and_audit(
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
) -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    writer = RecordingWriter()
    pipeline = build(gate, ledger=ledger, writer=writer)
    intent = make_intent(qty=Decimal("0.1"), created_at=now - timedelta(seconds=1))

    result = pipeline.process(intent, market_at)

    assert result.filled is True
    assert result.approved is not None
    assert result.approved_order is result.approved
    # Spot still runs the futures margin checks (they pass trivially at 1x).
    assert len(result.approved.risk_checks_passed) == 12
    assert result.trade is not None
    assert result.trade.fee == Decimal("1.5")

    assert [event.event_type for event in result.events] == [
        EventType.ORDER_APPROVED,
        EventType.ORDER_SUBMITTED,
        EventType.TRADE_RECORDED,
        EventType.POSITION_UPDATED,
    ]
    assert [event.event_type for event in writer.audits] == [
        event.event_type for event in result.events
    ]
    assert writer.orders[0][1].approval_id == result.approved.approval_id
    assert len(writer.trades) == 1
    assert writer.positions[0].qty == Decimal("0.1")

    assert ledger.position("BTC/USDT").qty == Decimal("0.1")
    assert ledger.equity == Decimal("9998.5")


def test_a_rejected_intent_never_reaches_the_broker(
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
) -> None:
    broker = SpyBroker()
    writer = RecordingWriter()
    pipeline = build(gate, broker=broker, writer=writer)
    intent = make_intent(qty=Decimal("1"), created_at=now - timedelta(seconds=1))

    result = pipeline.process(intent, market_at)

    assert broker.calls == 0
    assert result.approved is None
    assert result.execution is None
    assert result.trade is None
    assert writer.orders == []
    assert writer.audits[0].event_type is EventType.RISK_REJECTED
    assert any(reason.startswith("risk_per_trade_exceeded") for reason in result.reasons)


def test_stale_quotes_and_kill_switch_both_block_execution(
    limits: RiskLimit,
    make_intent: Callable[..., OrderIntent],
    now: datetime,
) -> None:
    stale_market = MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT",
                price=Decimal("30000"),
                as_of=now - timedelta(seconds=300),
            ),
        ),
        now=now,
    )
    intent = make_intent(qty=Decimal("0.1"), created_at=now - timedelta(seconds=1))

    stale = build(RiskGate(limits=limits, stale_after_seconds=60)).process(intent, stale_market)
    assert stale.approved is None
    assert any(reason.startswith("stale_market_data") for reason in stale.reasons)

    halted = build(RiskGate(limits=limits, kill_switch=True)).process(intent, now_market(now))
    assert halted.approved is None
    assert "kill_switch_active" in halted.reasons


def test_untouched_limit_is_recorded_but_changes_nothing(
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    now: datetime,
) -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    writer = RecordingWriter()
    pipeline = build(gate, ledger=ledger, writer=writer)
    intent = make_intent(qty=Decimal("0.1"), created_at=now - timedelta(seconds=1))

    result = pipeline.process(intent, now_market(now, price="31000"))

    assert result.filled is False
    assert result.trade is None
    assert [event.event_type for event in result.events] == [
        EventType.ORDER_APPROVED,
        EventType.ORDER_NOT_FILLED,
    ]
    assert ledger.position("BTC/USDT").is_flat
    assert ledger.realized_events == ()
    assert len(writer.orders) == 1
    assert writer.trades == []


def test_pipeline_works_without_a_writer(
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
) -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = build(gate, ledger=ledger)

    result = pipeline.process(
        make_intent(qty=Decimal("0.1"), created_at=now - timedelta(seconds=1)), market_at
    )

    assert result.filled is True
    assert ledger.position("BTC/USDT").qty == Decimal("0.1")


def test_market_context_older_than_the_intent_is_refused(
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
) -> None:
    pipeline = build(gate)
    future_intent = make_intent(qty=Decimal("0.1"), created_at=now + timedelta(seconds=60))

    with pytest.raises(ExecutionError, match="older than the intent"):
        pipeline.process(future_intent, market_at)


def test_the_ledger_is_what_limits_the_next_intent(
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
) -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = build(gate, ledger=ledger)

    first = pipeline.process(
        make_intent(qty=Decimal("0.1"), created_at=now - timedelta(seconds=1)), market_at
    )
    assert first.filled is True

    second = pipeline.process(
        make_intent(qty=Decimal("0.5"), created_at=now - timedelta(seconds=1)), market_at
    )

    assert second.approved is None
    assert any(reason.startswith("max_exposure_per_symbol_exceeded") for reason in second.reasons)


def now_market(now: datetime, *, price: str = "30000") -> MarketContext:
    return MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT",
                price=Decimal(price),
                as_of=now - timedelta(seconds=5),
            ),
        ),
        now=now,
    )


def test_trade_side_is_carried_through(
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
) -> None:
    pipeline = build(gate)

    result = pipeline.process(
        make_intent(side=Side.BUY, qty=Decimal("0.1"), created_at=now - timedelta(seconds=1)),
        market_at,
    )

    assert result.trade is not None
    assert result.trade.side is Side.BUY


class VenueProtectingBroker(PaperBroker):
    """A futures-like broker: fills locally **and** records venue-side exits."""

    name = "binance-futures-testnet"
    live = True

    def __init__(self) -> None:
        super().__init__()
        self.protected: list[dict[str, object]] = []
        self.cancelled: list[str] = []

    def submit_protection_order(self, **kwargs: object) -> SimpleNamespace:
        self.protected.append(kwargs)
        return SimpleNamespace(stop_order_id="9001", take_order_id="9002")

    def cancel_protection_orders(self, symbol: str) -> int:
        self.cancelled.append(symbol)
        return 1


def test_a_futures_fill_places_the_stop_on_the_venue(
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
) -> None:
    broker = VenueProtectingBroker()
    writer = RecordingWriter()
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = TradingPipeline(
        gate=gate,
        gateway=ExecutionGateway(broker, allow_live_orders=True),
        ledger=ledger,
        writer=writer,
    )
    intent = make_intent(qty=Decimal("0.1"), created_at=now - timedelta(seconds=1))

    result = pipeline.process(intent, market_at)

    assert result.filled is True
    assert len(broker.protected) == 1
    placed = broker.protected[0]
    # Closing a long is a SELL on the LONG leg: the side alone cannot say that.
    assert placed["side"] is Side.SELL
    assert placed["position_side"] is PositionSide.LONG
    assert placed["stop_loss"] == intent.stop_loss
    assert placed["take_profit"] == intent.take_profit
    assert EventType.PROTECTION_PLACED in [event.event_type for event in result.events]
    # The stop is persisted with the position, not only kept in memory.
    assert writer.positions[0].stop_loss == intent.stop_loss


def test_closing_a_position_cancels_the_venue_exit(
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
) -> None:
    broker = VenueProtectingBroker()
    writer = RecordingWriter()
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = TradingPipeline(
        gate=gate,
        gateway=ExecutionGateway(broker, allow_live_orders=True),
        ledger=ledger,
        writer=writer,
    )
    opening = make_intent(qty=Decimal("0.1"), created_at=now - timedelta(seconds=2))
    pipeline.process(opening, market_at)

    closing = make_intent(
        side=Side.SELL,
        qty=Decimal("0.1"),
        stop_loss=Decimal("30600"),
        take_profit=Decimal("28800"),
        created_at=now - timedelta(seconds=1),
    )
    result = pipeline.process(closing, market_at)

    assert result.filled is True
    assert broker.cancelled == ["BTC/USDT"]
    assert ledger.position("BTC/USDT").is_flat


class _FailingProtection(VenueProtectingBroker):
    def submit_protection_order(self, **kwargs: object) -> SimpleNamespace:
        raise ExecutionError("exchange refused the stop")


def test_a_rejected_stop_does_not_undo_the_fill(
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
) -> None:
    broker = _FailingProtection()
    writer = RecordingWriter()
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = TradingPipeline(
        gate=gate,
        gateway=ExecutionGateway(broker, allow_live_orders=True),
        ledger=ledger,
        writer=writer,
    )

    result = pipeline.process(
        make_intent(qty=Decimal("0.1"), created_at=now - timedelta(seconds=1)), market_at
    )

    assert result.filled is True
    assert ledger.position("BTC/USDT").qty == Decimal("0.1")
    assert EventType.PROTECTION_FAILED in [event.event_type for event in result.events]
