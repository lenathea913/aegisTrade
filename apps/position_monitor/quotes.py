"""Where the monitor gets the latest price.

A quote is the most recent *closed* candle for the symbol: the monitor must never act
on a bar that is still forming. ``CandleQuoteSource`` reads ``market.candles``; tests
pass any object with a ``latest`` method.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aegis_core import MarketQuote
from aegis_db import Candle as CandleRow
from aegis_db import session_scope

__all__ = ["CandleQuoteSource", "QuoteSource"]


class QuoteSource(Protocol):
    """Anything that can answer "what is the latest price for this symbol?"."""

    def latest(self, symbol: str) -> MarketQuote | None:
        """Latest closed price, or ``None`` when no candle exists yet."""


class CandleQuoteSource:
    """Reads the newest stored candle for a symbol."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def latest(self, symbol: str) -> MarketQuote | None:
        with session_scope(self._factory) as session:
            row = (
                session.execute(
                    select(CandleRow)
                    .where(CandleRow.symbol == symbol)
                    .order_by(CandleRow.close_time.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
        if row is None:
            return None
        return MarketQuote(symbol=row.symbol, price=row.close, as_of=row.close_time)
