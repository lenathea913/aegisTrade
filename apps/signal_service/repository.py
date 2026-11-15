"""Candle access for signal generation.

The service reads only *closed* candles from ``market.candles``; it never fetches from
an exchange, so a signal can always be explained by data the platform stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aegis_core import Candle, Timeframe
from aegis_db import Candle as CandleRow
from aegis_db import session_scope

__all__ = ["CandleRepository", "SqlCandleRepository"]


class CandleRepository(Protocol):
    """Recent candles for one symbol/timeframe, oldest first."""

    def recent(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[Candle]:
        """Return up to ``limit`` most recent closed candles."""


class SqlCandleRepository:
    """Reads ``market.candles``; one short session per call."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def recent(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[Candle]:
        with session_scope(self._factory) as session:
            rows = (
                session.execute(
                    select(CandleRow)
                    .where(CandleRow.symbol == symbol, CandleRow.timeframe == timeframe.value)
                    .order_by(CandleRow.open_time.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        return tuple(
            Candle(
                symbol=row.symbol,
                timeframe=Timeframe(row.timeframe),
                open_time=row.open_time,
                close_time=row.close_time,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                trades=row.trades,
            )
            for row in reversed(rows)
        )
