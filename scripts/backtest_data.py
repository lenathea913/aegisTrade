"""Candles for the research scripts, from the database or the offline archive cache.

Two sources, one shape:

* **database** — ``market.candles`` for the configured environment. This is the series
  the runner actually trades, so it is the default for "what would the bot have done".
  It only holds what the collector/backfill actually downloaded (the seven newer
  symbols have ~2 000 hourly bars, about 83 days).
* **cache** — the monthly 1h archives already downloaded by
  ``artifacts/top10_backtest.py`` into ``artifacts/universe_cache``. No network access:
  a missing file is simply skipped, so a symbol starts at its listing date. This is
  the only source with three years of history for all ten symbols.

``resample`` builds a higher timeframe from 1h bars instead of downloading it again:
each bucket must be complete, so an edge bucket that straddles the start or end of the
series is dropped rather than treated as a full bar.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from aegis_core import Candle, Timeframe

__all__ = [
    "CACHE_DIR",
    "cache_available",
    "load_from_cache",
    "load_from_database",
    "resample",
]

CACHE_DIR = Path("artifacts", "universe_cache")

#: How many 1h bars a bucket of each timeframe needs.
_HOURS_PER_BAR = {"1h": 1, "4h": 4, "1d": 24}
_SECONDS_PER_HOUR = 3600


def _to_datetime(stamp: int) -> datetime:
    """Binance stamps are microseconds today, milliseconds historically."""
    if stamp > 10**17:
        seconds = stamp / 1e9
    elif stamp > 10**14:
        seconds = stamp / 1e6
    elif stamp > 10**11:
        seconds = stamp / 1e3
    else:
        seconds = float(stamp)
    return datetime.fromtimestamp(seconds, UTC)


def load_from_database(symbol: str, timeframe: Timeframe, environment: str) -> list[Candle]:
    """Every stored bar for ``symbol``/``timeframe``, oldest first."""
    from sqlalchemy import select

    from aegis_config import load_settings
    from aegis_db import Candle as CandleRow
    from aegis_db import create_engine_from_settings, create_session_factory, session_scope

    settings = load_settings()
    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            rows = (
                session.execute(
                    select(CandleRow)
                    .where(
                        CandleRow.symbol == symbol,
                        CandleRow.timeframe == timeframe.value,
                        CandleRow.environment == environment,
                    )
                    .order_by(CandleRow.open_time)
                )
                .scalars()
                .all()
            )
            return [
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
                for row in rows
            ]
    finally:
        engine.dispose()


def cache_available(directory: Path = CACHE_DIR) -> bool:
    """True when at least one monthly 1h archive is already on disk."""
    return directory.is_dir() and any(directory.glob("*-1h-m-*.zip"))


def load_from_cache(
    symbol: str,
    start: datetime,
    end: datetime,
    directory: Path = CACHE_DIR,
) -> list[Candle]:
    """Hourly bars for ``symbol`` from the cached monthly archives, inside the window."""
    ticker = symbol.replace("/", "")
    candles: list[Candle] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        path = directory / f"{ticker}-1h-m-{year}-{month:02d}.zip"
        month += 1
        if month > 12:
            year, month = year + 1, 1
        if not path.exists():
            continue
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith(".csv"):
                    continue
                text = archive.read(name).decode("utf-8", "replace")
                for row in csv.reader(io.StringIO(text)):
                    if not row or not row[0].isdigit():
                        continue
                    open_time = _to_datetime(int(row[0]))
                    if open_time < start or open_time > end:
                        continue
                    candles.append(
                        Candle(
                            symbol=symbol,
                            timeframe=Timeframe.H1,
                            open_time=open_time,
                            close_time=_to_datetime(int(row[6])),
                            open=Decimal(row[1]),
                            high=Decimal(row[2]),
                            low=Decimal(row[3]),
                            close=Decimal(row[4]),
                            volume=Decimal(row[5]),
                            trades=int(row[8]) if row[8].isdigit() else None,
                        )
                    )
    candles.sort(key=lambda candle: candle.open_time)
    return candles


def resample(candles: Sequence[Candle], target: Timeframe) -> list[Candle]:
    """Aggregate hourly bars into ``target``, keeping only complete buckets.

    Buckets are aligned to the UTC epoch (00:00, 04:00, ...), which is how the venue
    aligns a 4h candle, so a resampled bar covers exactly the same window the
    exchange's own 4h candle would.
    """
    if not candles:
        return []
    hours = _HOURS_PER_BAR[target.value]
    if hours == 1:
        return list(candles)
    seconds = hours * _SECONDS_PER_HOUR
    buckets: dict[int, list[Candle]] = {}
    for candle in candles:
        key = int(candle.open_time.timestamp()) // seconds
        buckets.setdefault(key, []).append(candle)

    resampled: list[Candle] = []
    for key in sorted(buckets):
        bars = buckets[key]
        if len(bars) != hours:
            continue  # partial bucket at an edge: not a real candle
        first, last = bars[0], bars[-1]
        trades = [bar.trades for bar in bars if bar.trades is not None]
        resampled.append(
            Candle(
                symbol=first.symbol,
                timeframe=target,
                open_time=first.open_time,
                close_time=last.close_time,
                open=first.open,
                high=max(bar.high for bar in bars),
                low=min(bar.low for bar in bars),
                close=last.close,
                volume=sum((bar.volume for bar in bars), Decimal("0")),
                trades=sum(trades) if trades else None,
            )
        )
    return resampled
