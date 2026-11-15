"""Risk inputs shared between the ledger, collector and risk engine.

``PortfolioState`` is what the ledger will hand over (FASE 7) and
``MarketContext`` is what the collector/API feed (FASE 6/8). Both are immutable
snapshots: the risk engine judges one frozen view of the world, never a live query.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from aegis_core.base import StrictModel
from aegis_core.clock import utc_now
from aegis_core.position import Position
from aegis_core.types import FiniteDecimal, PositiveDecimal, SymbolStr, UtcDatetime

__all__ = ["MarketContext", "MarketQuote", "PortfolioState"]

_ZERO = Decimal("0")


class PortfolioState(StrictModel):
    """Equity, realized PnL for the loss windows, and current positions.

    ``equity`` is the risk base: every percentage limit is applied to it. Realized
    PnL is signed (negative = loss) and covers only the current day/week; unrealized
    PnL is deliberately excluded so the engine cannot be talked into extra size by
    an open winner.
    """

    equity: PositiveDecimal
    daily_realized_pnl: FiniteDecimal = _ZERO
    weekly_realized_pnl: FiniteDecimal = _ZERO
    positions: tuple[Position, ...] = ()

    @model_validator(mode="after")
    def _one_position_per_symbol(self) -> Self:
        symbols = [position.symbol for position in self.positions]
        duplicates = {symbol for symbol in symbols if symbols.count(symbol) > 1}
        if duplicates:
            raise ValueError(f"duplicate positions for symbols: {sorted(duplicates)}")
        return self

    def position(self, symbol: str) -> Position:
        """The position for ``symbol``; a flat position when none is held."""
        from aegis_core.types import normalise_symbol

        wanted = normalise_symbol(symbol)
        for position in self.positions:
            if position.symbol == wanted:
                return position
        return Position.flat(wanted)

    @property
    def open_positions(self) -> tuple[Position, ...]:
        return tuple(position for position in self.positions if not position.is_flat)


class MarketQuote(StrictModel):
    """Latest price for one symbol, with the time it was observed."""

    symbol: SymbolStr
    price: PositiveDecimal
    as_of: UtcDatetime = Field(default_factory=utc_now)


class MarketContext(StrictModel):
    """All quotes the engine may use, plus the decision timestamp."""

    quotes: tuple[MarketQuote, ...] = ()
    now: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _one_quote_per_symbol(self) -> Self:
        symbols = [quote.symbol for quote in self.quotes]
        duplicates = {symbol for symbol in symbols if symbols.count(symbol) > 1}
        if duplicates:
            raise ValueError(f"duplicate quotes for symbols: {sorted(duplicates)}")
        return self

    def quote(self, symbol: str) -> MarketQuote | None:
        from aegis_core.types import normalise_symbol

        wanted = normalise_symbol(symbol)
        for quote in self.quotes:
            if quote.symbol == wanted:
                return quote
        return None

    def price(self, symbol: str) -> Decimal | None:
        quote = self.quote(symbol)
        return None if quote is None else quote.price

    def age_seconds(self, symbol: str) -> float | None:
        """Seconds since the quote was observed, or ``None`` when there is no quote."""
        quote = self.quote(symbol)
        if quote is None:
            return None
        return (self.now - quote.as_of).total_seconds()

    def is_fresh(self, symbol: str, max_age_seconds: int) -> bool:
        """True when a quote exists, is not from the future, and is recent enough."""
        age = self.age_seconds(symbol)
        if age is None:
            return False
        return 0 <= age <= max_age_seconds
