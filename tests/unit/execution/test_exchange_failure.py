"""A broken exchange must fail safely: audit it, place nothing, leave the book flat.

This is the CI drill for FASE 13's "no hanging positions" requirement. The broker is a
fake that raises the same typed errors the testnet adapter raises on a timeout or a 401.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from aegis_core import (
    ApprovedOrder,
    MarketContext,
    MarketQuote,
    Order,
    OrderIntent,
    Position,
    Trade,
)
from aegis_execution import (
    Broker,
    BrokerFill,
    ExchangeAuthError,
    ExchangeUnavailableError,
    ExecutionGateway,
    LiveTradingDisabledError,
    TradingPipeline,
)
from aegis_ledger import AuditEvent, EventType, PortfolioLedger
from aegis_risk import RiskGate

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FailingBroker:
    """A live-venue broker whose exchange is unreachable."""

    name = "binance-testnet"
    live = True

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def submit(
        self, order: Order, *, market_price: Decimal, now: datetime | None = None
    ) -> BrokerFill | None:
        self.calls += 1
        raise self.error


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


def pipeline_for(
    broker: Broker, *, allow_live: bool = True
) -> tuple[TradingPipeline, RecordingWriter, PortfolioLedger]:
    writer = RecordingWriter()
    return (
        TradingPipeline(
            gate=RiskGate(limits=_limits()),
            gateway=ExecutionGateway(broker, allow_live_orders=allow_live),
            ledger=PortfolioLedger(initial_equity=Decimal("10000")),
            writer=writer,
        ),
        writer,
        PortfolioLedger(initial_equity=Decimal("10000")),
    )


def test_gateway_refuses_a_live_broker_without_the_extra_switch(
    make_intent: Callable[..., OrderIntent],
) -> None:
    gateway = ExecutionGateway(FailingBroker(ExchangeUnavailableError("down")))

    with pytest.raises(LiveTradingDisabledError):
        gateway.execute(
            ApprovedOrder(intent=make_intent(), max_risk_amount=Decimal("100")),
            market=MarketContext(
                quotes=(MarketQuote(symbol="BTC/USDT", price=Decimal("30000"), as_of=NOW),),
                now=NOW,
            ),
        )


@pytest.mark.parametrize(
    "error",
    [
        ExchangeUnavailableError("exchange timed out on /api/v3/order"),
        ExchangeAuthError("exchange rejected the credentials on /api/v3/order (HTTP 401)"),
    ],
)
def test_exchange_failure_leaves_no_position_and_no_trade(
    error: Exception,
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
) -> None:
    broker = FailingBroker(error)
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

    assert broker.calls == 1
    assert result.filled is False
    assert result.trade is None
    assert result.execution is None
    assert result.error is not None and str(error) in result.error

    # Nothing reached the ledger, and the order did not silently vanish.
    assert ledger.position("BTC/USDT").is_flat
    assert ledger.realized_events == ()
    assert writer.trades == []
    assert writer.positions == []
    # No Order object exists when the venue fails inside submit, so the attempt lives
    # in the audit trail (ORDER_NOT_FILLED + reason=exchange_error) and the logs.
    assert writer.orders == []

    failure = [event for event in result.events if event.event_type is EventType.ORDER_NOT_FILLED]
    assert len(failure) == 1
    assert failure[0].payload["reason"] == "exchange_error"
    assert failure[0].payload["error"]
    assert any(event.event_type is EventType.ORDER_APPROVED for event in result.events)


def _limits() -> Any:
    from aegis_core import RiskLimit

    return RiskLimit(
        max_risk_per_trade_pct=Decimal("2"),
        max_daily_loss_pct=Decimal("5"),
        max_weekly_loss_pct=Decimal("10"),
        max_exposure_per_symbol_pct=Decimal("100"),
        max_total_exposure_pct=Decimal("100"),
        max_leverage=Decimal("1"),
        allowed_symbols=frozenset({"BTC/USDT"}),
    )
