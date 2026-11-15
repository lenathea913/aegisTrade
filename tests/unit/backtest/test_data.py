"""CSV loading and the committed synthetic seed data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from aegis_backtest import load_candles_csv
from aegis_core import Timeframe

ROOT = Path(__file__).resolve().parents[3]
SEED = ROOT / "data" / "seeds" / "BTCUSDT_1h.csv"

HEADER = "open_time,open,high,low,close,volume\n"
ROWS = "2024-01-01T01:00:00Z,101,102,100,101.5,10\n2024-01-01T00:00:00Z,100,101,99,100.5,12\n"


def write_csv(tmp_path: Path, body: str, header: str = HEADER) -> Path:
    path = tmp_path / "candles.csv"
    path.write_text(header + body, encoding="utf-8")
    return path


def test_rows_are_validated_and_sorted(tmp_path: Path) -> None:
    candles = load_candles_csv(write_csv(tmp_path, ROWS), symbol="btc-usdt", timeframe=Timeframe.H1)

    assert len(candles) == 2
    assert [candle.open_time.hour for candle in candles] == [0, 1]
    assert candles[0].symbol == "BTC/USDT"
    assert candles[0].close_time == datetime(2024, 1, 1, 1, tzinfo=UTC)
    assert candles[0].close == Decimal("100.5")
    assert candles[0].volume == Decimal("12")


def test_explicit_close_time_column_is_used(tmp_path: Path) -> None:
    header = "open_time,close_time,open,high,low,close,volume\n"
    body = "2024-01-01T00:00:00Z,2024-01-01T00:30:00Z,100,101,99,100.5,1\n"
    path = write_csv(tmp_path, body, header)

    (candle,) = load_candles_csv(path, symbol="BTC/USDT", timeframe=Timeframe.H1)

    assert candle.close_time == datetime(2024, 1, 1, 0, 30, tzinfo=UTC)


def test_missing_column_is_reported(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "", header="open_time,open,high,low,close\n")

    with pytest.raises(ValueError, match="missing columns"):
        load_candles_csv(path, symbol="BTC/USDT", timeframe=Timeframe.H1)


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_candles_csv(tmp_path / "nope.csv", symbol="BTC/USDT", timeframe=Timeframe.H1)


def test_invalid_decimal_is_reported(tmp_path: Path) -> None:
    body = "2024-01-01T00:00:00Z,abc,101,99,100.5,12\n"

    with pytest.raises(ValueError, match="invalid candle row"):
        load_candles_csv(write_csv(tmp_path, body), symbol="BTC/USDT", timeframe=Timeframe.H1)


def test_naive_timestamp_is_rejected(tmp_path: Path) -> None:
    body = "2024-01-01 00:00:00,100,101,99,100.5,12\n"

    with pytest.raises(ValueError, match="timezone"):
        load_candles_csv(write_csv(tmp_path, body), symbol="BTC/USDT", timeframe=Timeframe.H1)


def test_committed_synthetic_seed_is_valid() -> None:
    candles = load_candles_csv(SEED, symbol="BTC/USDT", timeframe=Timeframe.H1)

    assert len(candles) == 1500
    assert candles[0].open_time == datetime(2024, 1, 1, tzinfo=UTC)
    assert candles[-1].close_time - candles[0].open_time == timedelta(hours=1500)
    for earlier, later in pairwise(candles):
        assert later.open_time == earlier.open_time + timedelta(hours=1)
