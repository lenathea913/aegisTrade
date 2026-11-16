"""Collector orchestration: totals, lag, isolation of failures, retries, idempotency."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aegis_core import Candle, Timeframe
from market_collector.collector import MarketCollector
from market_collector.retry import RetryPolicy
from market_collector.sources import CandleSource, CollectorError
from market_collector.store import CandleWriter

START = datetime(2024, 1, 1, tzinfo=UTC)
NOW = datetime(2024, 1, 1, 3, 5, tzinfo=UTC)  # 5 minutes after the last close


def make_candle(hour: int, symbol: str = "BTC/USDT") -> Candle:
    open_time = START + timedelta(hours=hour)
    return Candle(
        symbol=symbol,
        timeframe=Timeframe.H1,
        open_time=open_time,
        close_time=open_time + timedelta(hours=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("5"),
    )


BTC = [make_candle(0), make_candle(1), make_candle(2)]
ETH = [make_candle(0, "ETH/USDT"), make_candle(1, "ETH/USDT")]


class ListSource:
    label = "csv"

    def __init__(self, series: dict[str, Sequence[Candle]], failing: frozenset[str] = frozenset()):
        self._series = series
        self._failing = failing
        self.calls: list[tuple[str, Timeframe, int]] = []

    def fetch(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[Candle]:
        self.calls.append((symbol, timeframe, limit))
        if symbol in self._failing:
            raise CollectorError(f"no data for {symbol}")
        return self._series.get(symbol, ())


class FlakySource:
    label = "csv"

    def __init__(self, series: Sequence[Candle], failures: int) -> None:
        self._series = series
        self.remaining = failures

    def fetch(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[Candle]:
        if self.remaining > 0:
            self.remaining -= 1
            raise CollectorError("temporary outage")
        return self._series


class RecordingWriter:
    def __init__(self) -> None:
        self.seen: set[tuple[str, str, datetime]] = set()
        self.calls: list[tuple[str, str, int]] = []

    def upsert(
        self, candles: Sequence[Candle], *, exchange: str, source: str, environment: str
    ) -> int:
        self.calls.append((exchange, source, len(candles)))
        new = 0
        for candle in candles:
            key = (candle.symbol, candle.timeframe.value, candle.open_time)
            if key not in self.seen:
                self.seen.add(key)
                new += 1
        return new


def build(
    source: CandleSource,
    writer: CandleWriter,
    *,
    retry: RetryPolicy | None = None,
) -> MarketCollector:
    return MarketCollector(
        source=source,
        store=writer,
        symbols=("BTC/USDT",),
        timeframes=(Timeframe.H1,),
        now=lambda: NOW,
        retry=retry,
    )


def test_run_reports_totals_and_lag() -> None:
    writer = RecordingWriter()
    collector = build(ListSource({"BTC/USDT": BTC}), writer)

    summary = collector.run_once()

    assert summary.fetched == 3
    assert summary.inserted == 3
    assert summary.failures == ()
    assert summary.max_lag_seconds == 300.0  # last close 03:00, now 03:05


def test_running_twice_is_idempotent() -> None:
    writer = RecordingWriter()
    collector = build(ListSource({"BTC/USDT": BTC}), writer)

    first = collector.run_once()
    second = collector.run_once()

    assert first.inserted == 3
    assert second.inserted == 0
    assert len(writer.seen) == 3


def test_multiple_symbols_share_one_run() -> None:
    writer = RecordingWriter()
    collector = MarketCollector(
        source=ListSource({"BTC/USDT": BTC, "ETH/USDT": ETH}),
        store=writer,
        symbols=("BTC/USDT", "ETH/USDT"),
        timeframes=(Timeframe.H1,),
        now=lambda: NOW,
    )

    summary = collector.run_once()

    assert len(summary.series) == 2
    assert summary.fetched == 5


def test_one_failing_series_does_not_abort_the_run() -> None:
    writer = RecordingWriter()
    collector = MarketCollector(
        source=ListSource({"BTC/USDT": BTC, "ETH/USDT": ETH}, failing=frozenset({"ETH/USDT"})),
        store=writer,
        symbols=("BTC/USDT", "ETH/USDT"),
        timeframes=(Timeframe.H1,),
        now=lambda: NOW,
    )

    summary = collector.run_once()

    assert len(summary.failures) == 1
    assert summary.failures[0].symbol == "ETH/USDT"
    assert "no data" in (summary.failures[0].error or "")
    assert summary.inserted == 3  # BTC still stored


def test_empty_source_result_is_reported() -> None:
    collector = build(ListSource({}), RecordingWriter())

    summary = collector.run_once()

    assert summary.failures[0].error == "source returned no candles"


def test_store_failures_are_reported() -> None:
    class BrokenWriter:
        def upsert(
            self, candles: Sequence[Candle], *, exchange: str, source: str, environment: str
        ) -> int:
            raise RuntimeError("database unavailable")

    summary = build(ListSource({"BTC/USDT": BTC}), BrokenWriter()).run_once()

    assert "store failed" in (summary.failures[0].error or "")
    assert summary.failures[0].fetched == 3


def test_transient_failures_are_retried_with_backoff() -> None:
    sleeps: list[float] = []
    writer = RecordingWriter()
    collector = build(
        FlakySource(BTC, failures=2),
        writer,
        retry=RetryPolicy(attempts=3, initial_delay=1.0, sleep=sleeps.append),
    )

    summary = collector.run_once()

    assert summary.failures == ()
    assert summary.inserted == 3
    assert sleeps == [1.0, 2.0]


def test_exhausted_retries_surface_the_error() -> None:
    sleeps: list[float] = []
    collector = build(
        FlakySource(BTC, failures=5),
        RecordingWriter(),
        retry=RetryPolicy(attempts=2, initial_delay=2.0, sleep=sleeps.append),
    )

    summary = collector.run_once()

    assert summary.max_lag_seconds is None
    assert "temporary outage" in (summary.failures[0].error or "")
    assert sleeps == [2.0]


def test_collector_requires_symbols_and_timeframes() -> None:
    with pytest.raises(ValueError, match="symbol"):
        MarketCollector(
            source=ListSource({}), store=RecordingWriter(), symbols=(), timeframes=(Timeframe.H1,)
        )
    with pytest.raises(ValueError, match="timeframe"):
        MarketCollector(
            source=ListSource({}), store=RecordingWriter(), symbols=("BTC/USDT",), timeframes=()
        )
