"""The collector worker.

One run walks every (symbol, timeframe) pair, fetches the latest closed candles with
retry, and upserts them idempotently. A failing series never aborts the run: it is
reported in the summary so the operator sees a partial failure instead of a silent
stall.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from aegis_core import Timeframe, utc_now
from market_collector.retry import RetryPolicy
from market_collector.sources import CandleSource
from market_collector.store import CandleWriter

__all__ = ["CollectionSummary", "MarketCollector", "SeriesResult"]

DEFAULT_LIMIT = 1000


@dataclass(frozen=True)
class SeriesResult:
    """Outcome for one symbol/timeframe pair."""

    symbol: str
    timeframe: Timeframe
    fetched: int = 0
    inserted: int = 0
    lag_seconds: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class CollectionSummary:
    """Outcome of one full run."""

    series: tuple[SeriesResult, ...]
    started_at: datetime

    @property
    def fetched(self) -> int:
        return sum(result.fetched for result in self.series)

    @property
    def inserted(self) -> int:
        return sum(result.inserted for result in self.series)

    @property
    def failures(self) -> tuple[SeriesResult, ...]:
        return tuple(result for result in self.series if not result.ok)

    @property
    def max_lag_seconds(self) -> float | None:
        lags = [result.lag_seconds for result in self.series if result.lag_seconds is not None]
        return max(lags) if lags else None


class MarketCollector:
    """Fetch-and-store worker for a fixed set of symbols and timeframes."""

    def __init__(
        self,
        *,
        source: CandleSource,
        store: CandleWriter,
        symbols: Sequence[str],
        timeframes: Sequence[Timeframe],
        exchange: str = "binance",
        environment: str = "testnet",
        limit: int = DEFAULT_LIMIT,
        retry: RetryPolicy | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if not symbols:
            raise ValueError("collector needs at least one symbol")
        if not timeframes:
            raise ValueError("collector needs at least one timeframe")
        self._source = source
        self._store = store
        self._symbols = tuple(symbols)
        self._timeframes = tuple(timeframes)
        self._exchange = exchange
        self._environment = environment
        self._limit = limit
        self._retry = retry or RetryPolicy()
        self._now = now

    @property
    def source_kind(self) -> str:
        return type(self._source).__name__

    def run_once(self) -> CollectionSummary:
        started_at = self._now()
        results: list[SeriesResult] = []

        for symbol in self._symbols:
            for timeframe in self._timeframes:
                results.append(self._collect_series(symbol, timeframe, started_at))

        return CollectionSummary(series=tuple(results), started_at=started_at)

    def _collect_series(
        self, symbol: str, timeframe: Timeframe, started_at: datetime
    ) -> SeriesResult:
        try:
            candles = self._retry.call(lambda: self._source.fetch(symbol, timeframe, self._limit))
        except Exception as exc:
            return SeriesResult(symbol=symbol, timeframe=timeframe, error=str(exc))

        if not candles:
            return SeriesResult(
                symbol=symbol, timeframe=timeframe, error="source returned no candles"
            )

        try:
            inserted = self._store.upsert(
                candles,
                exchange=self._exchange,
                source=self._source.label,
                environment=self._environment,
            )
        except Exception as exc:
            return SeriesResult(
                symbol=symbol,
                timeframe=timeframe,
                fetched=len(candles),
                error=f"store failed: {exc}",
            )

        last_close = max(candle.close_time for candle in candles)
        lag = (started_at - last_close).total_seconds()

        return SeriesResult(
            symbol=symbol,
            timeframe=timeframe,
            fetched=len(candles),
            inserted=inserted,
            lag_seconds=lag,
        )
