"""Operator-initiated trading from the API: closing a position through the gate.

The HTTP layer is allowed exactly one mutation: asking to flatten a position the ledger
already holds. It is not allowed to open exposure — that stays with the runner and the
signal conversion path — so this module builds an opposite-side MARKET intent for the
whole quantity and hands it to ``TradingPipeline``.

Nothing here bypasses risk: the gate judges the intent like any other, recognises it as
risk-reducing (ADR 0009) and the decision, the fill and the ``manual_close`` audit event
land in the same transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from aegis_config import Settings
from aegis_core import (
    MarketContext,
    MarketQuote,
    OrderIntent,
    OrderType,
    Position,
    PositionSide,
    Side,
    utc_now,
)
from aegis_execution import ExecutionGateway, PipelineResult, TradingPipeline, build_broker
from aegis_ledger import AuditEvent, EventType, LedgerWriter, PortfolioLedger
from aegis_risk import RiskGate

__all__ = [
    "CloseRequestError",
    "build_close_intent",
    "close_position",
]

#: Distance between the mark and the stop on an operator close (0.2 %). It only has to
#: be non-zero: the intent is closing exposure, not opening it.
_CLOSE_STOP_BUFFER = Decimal("0.002")


class CloseRequestError(Exception):
    """The request cannot be carried out: nothing open, or no price to close at."""


class QuoteLookup(Protocol):
    """Anything that can answer "what is the latest price for this symbol?"."""

    def latest(self, symbol: str) -> MarketQuote | None: ...


def build_close_intent(position: Position, quote: MarketQuote, *, now: datetime) -> OrderIntent:
    """Opposite-side MARKET intent for the whole position, with a stop just beyond."""
    if position.is_flat:
        raise CloseRequestError(f"No open position for {position.symbol}.")

    closing_side = Side.SELL if position.qty > 0 else Side.BUY
    offset = quote.price * _CLOSE_STOP_BUFFER
    stop_loss = quote.price + offset if closing_side is Side.SELL else quote.price - offset
    return OrderIntent(
        symbol=position.symbol,
        side=closing_side,
        order_type=OrderType.MARKET,
        qty=position.abs_qty,
        price=quote.price,
        stop_loss=stop_loss,
        take_profit=None,
        strategy_id="manual_close",
        reason=f"manual close requested by the operator at {quote.price}",
        leverage=position.leverage,
        position_side=PositionSide.for_entry(Side.BUY if position.qty > 0 else Side.SELL),
        created_at=now,
    )


def close_position(
    *,
    settings: Settings,
    ledger: PortfolioLedger,
    quotes: QuoteLookup,
    writer: LedgerWriter,
    symbol: str,
    clock: Callable[[], datetime] = utc_now,
) -> PipelineResult:
    """Close ``symbol`` through ``TradingPipeline`` and record a ``manual_close`` event."""
    position = ledger.position(symbol)
    if position.is_flat:
        raise CloseRequestError(f"No open position for {symbol}.")
    quote = quotes.latest(symbol)
    if quote is None:
        raise CloseRequestError(f"No stored price for {symbol}; cannot close.")

    now = clock()
    intent = build_close_intent(position, quote, now=now)
    pipeline = TradingPipeline(
        gate=RiskGate.from_settings(settings, settings.market_data.timeframes[0]),
        gateway=ExecutionGateway(
            build_broker(
                settings.execution,
                exchange=settings.exchange,
                live_confirm=settings.live_confirm,
            ),
            allow_live_orders=settings.execution.allow_live_orders,
        ),
        ledger=ledger,
        writer=writer,
    )
    # The operator's request is part of the record even if the gate then refuses it.
    writer.append_audit(
        AuditEvent(
            event_type=EventType.MANUAL_CLOSE,
            entity_type="position",
            entity_id=symbol,
            actor="operator",
            payload={
                "symbol": symbol,
                "held_qty": str(position.qty),
                "closing_qty": str(intent.qty),
                "mark": str(quote.price),
                "source": "api",
            },
        )
    )
    market = MarketContext(quotes=(quote,), now=now)
    return pipeline.process(intent, market)
