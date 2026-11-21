"""Freshness check for the runner: the newest stored candle per symbol.

Signals are only tradeable while their data is current. The runner asks this source
what the newest candle is and compares its close time with
``market_data.stale_after_seconds`` before it does any other work, so a stalled
collector cannot produce orders against yesterday's prices.

Only ``market.candles`` is read; the runner never calls an exchange itself.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from aegis_core import Candle, Timeframe
from signal_service import SqlCandleRepository

__all__ = ["LatestCandleSource", "SqlLatestCandleSource"]


class LatestCandleSource(Protocol):
    """The one question the runner asks about market data."""

    def latest(self, symbol: str, timeframe: Timeframe) -> Candle | None:
        """Newest stored candle for ``symbol``/``timeframe``, or ``None``."""


class SqlLatestCandleSource:
    """Reads the newest row through ``SqlCandleRepository`` (one short session)."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        environment: str = "testnet",
        venue: str = "spot",
    ) -> None:
        self._candles = SqlCandleRepository(factory, environment=environment, venue=venue)

    def latest(self, symbol: str, timeframe: Timeframe) -> Candle | None:
        rows = self._candles.recent(symbol, timeframe, 1)
        return rows[-1] if rows else None
