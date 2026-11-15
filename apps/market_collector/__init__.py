"""Market data collector: OHLCV -> ``market.candles`` (idempotent, retried)."""

from __future__ import annotations

from market_collector.collector import CollectionSummary, MarketCollector, SeriesResult
from market_collector.retry import RetryPolicy, call_with_retry
from market_collector.sources import (
    BinanceKlineSource,
    CandleSource,
    CollectorError,
    CsvCandleSource,
    build_source,
)
from market_collector.store import CandleStore, CandleWriter

__all__ = [
    "BinanceKlineSource",
    "CandleSource",
    "CandleStore",
    "CandleWriter",
    "CollectionSummary",
    "CollectorError",
    "CsvCandleSource",
    "MarketCollector",
    "RetryPolicy",
    "SeriesResult",
    "build_source",
    "call_with_retry",
]
