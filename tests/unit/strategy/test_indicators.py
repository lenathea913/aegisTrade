"""ADX and Bollinger bands: the maths a filter leans on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aegis_core import Candle, Timeframe
from aegis_strategy import AdxTracker, adx, bollinger_bands

START = datetime(2024, 1, 1, tzinfo=UTC)


def series(closes: list[str]) -> list[Candle]:
    previous = Decimal(closes[0])
    candles: list[Candle] = []
    for index, raw in enumerate(closes):
        close = Decimal(raw)
        candles.append(
            Candle(
                symbol="BTC/USDT",
                timeframe=Timeframe.H1,
                open_time=START + timedelta(hours=index),
                close_time=START + timedelta(hours=index + 1),
                open=previous,
                high=max(previous, close) + Decimal("1"),
                low=min(previous, close) - Decimal("1"),
                close=close,
            )
        )
        previous = close
    return candles


# ------------------------------------------------------------------------ adx


def test_adx_does_not_exist_before_two_periods() -> None:
    tracker = AdxTracker(period=14)

    for candle in series([str(100 + index) for index in range(27)]):
        tracker.update(candle)

    assert tracker.value is None


def test_adx_is_high_in_a_steady_trend() -> None:
    candles = series([str(100 + index) for index in range(80)])

    value = adx(candles, 14)

    assert value is not None
    assert value > Decimal("50")


def test_adx_is_low_in_a_flat_market() -> None:
    candles = series(["100"] * 80)

    value = adx(candles, 14)

    assert value is None or value < Decimal("25")


def test_incremental_tracker_matches_a_replay() -> None:
    candles = series([str(100 + (index % 7)) for index in range(60)])
    tracker = AdxTracker(period=14)
    for candle in candles:
        tracker.update(candle)

    assert tracker.value == adx(candles, 14)


def test_adx_rejects_a_period_below_two() -> None:
    with pytest.raises(ValueError, match="period"):
        AdxTracker(period=1)


# ----------------------------------------------------------------- bollinger


def test_bollinger_bands_bracket_a_known_window() -> None:
    closes = [Decimal(value) for value in ("10", "10", "10", "10")]

    bands = bollinger_bands(closes, period=4, num_std=Decimal("2"))

    assert bands is not None
    assert bands.middle == Decimal("10")
    assert bands.lower == Decimal("10")  # zero dispersion: all three lines meet
    assert bands.upper == Decimal("10")


def test_bollinger_bands_need_a_full_window() -> None:
    closes = [Decimal("10"), Decimal("11")]

    assert bollinger_bands(closes, period=20) is None


def test_bollinger_bands_dispersion_widens_the_lines() -> None:
    closes = [Decimal("10"), Decimal("12"), Decimal("10"), Decimal("12")]

    bands = bollinger_bands(closes, period=4, num_std=Decimal("2"))

    assert bands is not None
    assert bands.middle == Decimal("11")
    assert bands.lower < Decimal("11") < bands.upper


@pytest.mark.parametrize(("period", "num_std"), [(1, "2"), (20, "0")])
def test_bollinger_rejects_invalid_parameters(period: int, num_std: str) -> None:
    with pytest.raises(ValueError):
        bollinger_bands([Decimal("1")] * 30, period=period, num_std=Decimal(num_std))
