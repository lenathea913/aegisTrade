"""Where the monitor gets the latest price.

A quote is the most recent *closed* candle for the symbol: the monitor must never act
on a bar that is still forming. ``CandleQuoteSource`` reads ``market.candles``; tests
pass any object with a ``latest`` method.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aegis_core import MarketQuote, utc_now
from aegis_db import Candle as CandleRow
from aegis_db import session_scope
from market_streamer.marks import MarkStore, RedisMarkStore, fresh

__all__ = [
    "CandleQuoteSource",
    "MarkStoreQuoteSource",
    "QuoteSource",
    "live_quote_source",
]


class QuoteSource(Protocol):
    """Anything that can answer "what is the latest price for this symbol?"."""

    def latest(self, symbol: str) -> MarketQuote | None:
        """Latest closed price, or ``None`` when no candle exists yet."""


class MarkStoreQuoteSource:
    """Reads real-time marks (Redis) and falls back to closed candles.

    ``max_age_seconds`` is the same TTL the streamer writes with: a mark older than that
    means the streamer stopped, and a stopped streamer must not keep a stale price in
    front of a stop-loss decision.
    """

    def __init__(
        self,
        store: MarkStore,
        *,
        max_age_seconds: float,
        fallback: QuoteSource | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._max_age = max_age_seconds
        self._fallback = fallback
        self._clock = clock

    def latest(self, symbol: str) -> MarketQuote | None:
        mark = self._store.latest(symbol)
        if mark is not None and fresh(mark, max_age_seconds=self._max_age, now=self._clock()):
            return MarketQuote(symbol=mark.symbol, price=mark.price, as_of=mark.observed_at)
        if self._fallback is not None:
            return self._fallback.latest(symbol)
        return None


def live_quote_source(settings: Any, factory: sessionmaker[Session]) -> QuoteSource | None:
    """Redis marks when ``use_live_price`` is on, otherwise ``None``.

    Building it never fails the process: a missing Redis or an unusable URL degrades to
    the closed-candle source the monitor already has.
    """
    if not settings.position_monitor.use_live_price:
        return None
    try:
        store = RedisMarkStore.from_url(
            settings.redis.url, ttl_seconds=settings.streaming.mark_ttl_seconds
        )
    except Exception:
        return None
    return MarkStoreQuoteSource(
        store,
        max_age_seconds=settings.streaming.mark_ttl_seconds,
        fallback=CandleQuoteSource(factory, environment=settings.exchange.environment),
    )


class CandleQuoteSource:
    """Reads the newest stored candle for a symbol, from one venue only."""

    def __init__(self, factory: sessionmaker[Session], environment: str = "testnet") -> None:
        self._factory = factory
        self._environment = environment

    def latest(self, symbol: str) -> MarketQuote | None:
        with session_scope(self._factory) as session:
            row = (
                session.execute(
                    select(CandleRow)
                    .where(
                        CandleRow.environment == self._environment,
                        CandleRow.symbol == symbol,
                    )
                    .order_by(CandleRow.close_time.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
        if row is None:
            return None
        return MarketQuote(symbol=row.symbol, price=row.close, as_of=row.close_time)
