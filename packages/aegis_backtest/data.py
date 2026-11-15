"""Reading candle data from CSV (seed/synthetic data).

CSV columns: ``open_time,open,high,low,close,volume[,trades]`` with ISO-8601
timestamps (``Z`` or explicit offset). Rows are validated into ``Candle`` objects,
so a malformed seed file fails loudly instead of silently skewing a backtest.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aegis_core import Candle, Timeframe

__all__ = ["TIMEFRAME_DELTAS", "load_candles_csv"]

_REQUIRED_COLUMNS = ("open_time", "open", "high", "low", "close", "volume")

TIMEFRAME_DELTAS: dict[Timeframe, timedelta] = {
    Timeframe.M1: timedelta(minutes=1),
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}


def _parse_time(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError(f"timestamp {raw!r} has no timezone; export UTC timestamps")
    return value.astimezone(UTC)


def load_candles_csv(
    path: Path | str,
    *,
    symbol: str,
    timeframe: Timeframe,
) -> tuple[Candle, ...]:
    """Load and validate candles for one symbol/timeframe.

    ``close_time`` is derived as ``open_time + timeframe`` unless the file has a
    ``close_time`` column. Output is sorted by ``open_time``.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"candle file not found: {file_path}")

    candles: list[Candle] = []
    with file_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [column for column in _REQUIRED_COLUMNS if column not in fieldnames]
        if missing:
            raise ValueError(f"{file_path} is missing columns: {missing}")
        has_close_time = "close_time" in fieldnames

        for row in reader:
            open_time = _parse_time(row["open_time"])
            close_time = (
                _parse_time(row["close_time"])
                if has_close_time
                else open_time + TIMEFRAME_DELTAS[timeframe]
            )
            try:
                candles.append(
                    Candle(
                        symbol=symbol,
                        timeframe=timeframe,
                        open_time=open_time,
                        close_time=close_time,
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=Decimal(row["volume"]),
                        trades=int(row["trades"]) if row.get("trades") else None,
                    )
                )
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"invalid candle row in {file_path}: {row}") from exc

    return tuple(sorted(candles, key=lambda candle: candle.open_time))
