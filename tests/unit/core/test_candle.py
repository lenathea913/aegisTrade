"""Candle: OHLC invariants, UTC timestamps, derived helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import Candle, Timeframe


def _candle(**overrides: object) -> Candle:
    payload: dict[str, object] = {
        "symbol": "btc-usdt",
        "timeframe": Timeframe.H1,
        "open_time": datetime(2024, 1, 1, 0, tzinfo=UTC),
        "close_time": datetime(2024, 1, 1, 1, tzinfo=UTC),
        "open": Decimal("100"),
        "high": Decimal("110"),
        "low": Decimal("95"),
        "close": Decimal("105"),
        "volume": Decimal("12.5"),
    }
    payload.update(overrides)
    return Candle(**payload)


def test_valid_candle_is_normalised() -> None:
    candle = _candle()

    assert candle.symbol == "BTC/USDT"
    assert candle.timeframe is Timeframe.H1
    assert candle.range == Decimal("15")
    assert candle.is_bullish is True
    assert candle.volume == Decimal("12.5")


def test_bearish_candle() -> None:
    assert _candle(close=Decimal("98")).is_bullish is False


def test_close_time_must_follow_open_time() -> None:
    with pytest.raises(ValidationError, match="close_time"):
        _candle(close_time=datetime(2024, 1, 1, 0, tzinfo=UTC))


def test_high_cannot_be_below_low() -> None:
    with pytest.raises(ValidationError, match="high"):
        _candle(high=Decimal("90"), low=Decimal("95"))


def test_open_and_close_must_lie_inside_the_range() -> None:
    with pytest.raises(ValidationError, match="within"):
        _candle(open=Decimal("120"))
    with pytest.raises(ValidationError, match="within"):
        _candle(close=Decimal("90"))


def test_prices_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _candle(low=Decimal("0"))


def test_volume_may_be_zero_but_not_negative() -> None:
    assert _candle(volume=Decimal("0")).volume == 0
    with pytest.raises(ValidationError):
        _candle(volume=Decimal("-1"))


def test_trades_must_be_non_negative() -> None:
    assert _candle(trades=10).trades == 10
    with pytest.raises(ValidationError):
        _candle(trades=-1)


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _candle(open_time=datetime(2024, 1, 1, 0))


def test_non_utc_timestamps_are_converted() -> None:
    jakarta = timezone(timedelta(hours=7))
    candle = _candle(
        open_time=datetime(2024, 1, 1, 7, tzinfo=jakarta),
        close_time=datetime(2024, 1, 1, 8, tzinfo=jakarta),
    )

    assert candle.open_time == datetime(2024, 1, 1, 0, tzinfo=UTC)
    assert candle.open_time.utcoffset() == timedelta(0)


def test_timeframe_must_be_known() -> None:
    with pytest.raises(ValidationError):
        _candle(timeframe="2h")
