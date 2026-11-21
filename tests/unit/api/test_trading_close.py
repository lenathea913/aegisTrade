"""Operator-initiated close: an opposite-side intent that still goes through the gate."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from aegis_config import load_settings
from aegis_core import (
    ApprovedOrder,
    MarketQuote,
    Order,
    Position,
    Side,
    Trade,
)
from aegis_ledger import AuditEvent, EventType, PortfolioLedger
from api.trading import CloseRequestError, close_position

SYMBOL = "BTC/USDT"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class StaticQuotes:
    def __init__(self, price: Decimal) -> None:
        self.price = price

    def latest(self, symbol: str) -> MarketQuote:
        return MarketQuote(symbol=symbol, price=self.price, as_of=NOW)


class RecordingWriter:
    """Collects what the pipeline would have persisted, in order."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.trades: list[Trade] = []
        self.positions: list[Position] = []

    def save_order(self, order: Order, approved: ApprovedOrder) -> None:
        return None

    def save_trade(self, trade: Trade) -> None:
        self.trades.append(trade)

    def save_position(self, position: Position) -> None:
        self.positions.append(position)

    def append_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


def open_long(ledger: PortfolioLedger, qty: str = "0.1") -> None:
    ledger.apply_trade(
        Trade(
            order_id="ord_open",
            symbol=SYMBOL,
            side=Side.BUY,
            qty=Decimal(qty),
            price=Decimal("30000"),
            fee=Decimal("0"),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
        )
    )


def test_close_flattens_the_position_and_records_manual_close() -> None:
    settings = load_settings("paper")
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    open_long(ledger)
    writer = RecordingWriter()

    result = close_position(
        settings=settings,
        ledger=ledger,
        quotes=StaticQuotes(Decimal("30500")),
        writer=writer,
        symbol=SYMBOL,
        clock=lambda: NOW,
    )

    assert result.decision.approved is True
    assert result.filled is True
    assert result.intent.side is Side.SELL
    assert result.intent.qty == Decimal("0.1")
    assert ledger.position(SYMBOL).is_flat
    assert writer.trades and writer.positions
    assert writer.events[0].event_type is EventType.MANUAL_CLOSE
    assert writer.events[0].actor == "operator"
    assert writer.events[0].payload["closing_qty"] == "0.1"


def test_closing_a_short_buys_it_back() -> None:
    settings = load_settings("paper")
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(
        Trade(
            order_id="ord_short",
            symbol=SYMBOL,
            side=Side.SELL,
            qty=Decimal("0.2"),
            price=Decimal("30000"),
            fee=Decimal("0"),
            filled_at=NOW,
            strategy_id="test",
        )
    )

    result = close_position(
        settings=settings,
        ledger=ledger,
        quotes=StaticQuotes(Decimal("29800")),
        writer=RecordingWriter(),
        symbol=SYMBOL,
        clock=lambda: NOW,
    )

    assert result.intent.side is Side.BUY
    assert ledger.position(SYMBOL).is_flat


def test_a_flat_book_has_nothing_to_close() -> None:
    with pytest.raises(CloseRequestError, match="No open position"):
        close_position(
            settings=load_settings("paper"),
            ledger=PortfolioLedger(initial_equity=Decimal("10000")),
            quotes=StaticQuotes(Decimal("30000")),
            writer=RecordingWriter(),
            symbol=SYMBOL,
            clock=lambda: NOW,
        )


def test_the_kill_switch_does_not_trap_the_operator_in_a_position() -> None:
    """A halted bot must still let the operator flatten what it holds."""
    base = load_settings("paper")
    settings = base.model_copy(update={"risk": base.risk.model_copy(update={"kill_switch": True})})
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    open_long(ledger)
    writer = RecordingWriter()

    result = close_position(
        settings=settings,
        ledger=ledger,
        quotes=StaticQuotes(Decimal("30000")),
        writer=writer,
        symbol=SYMBOL,
        clock=lambda: NOW,
    )

    assert result.decision.approved is True
    assert "kill_switch_allows_exit" in result.reasons
    assert result.filled is True
    assert ledger.position(SYMBOL).is_flat is True
    assert writer.events[0].event_type is EventType.MANUAL_CLOSE
