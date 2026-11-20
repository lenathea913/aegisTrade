"""Bollinger mean reversion: entries only when price crosses into a band."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import Candle, OrderIntent, Side, Timeframe
from aegis_strategy import BollingerParams, BollingerReversionStrategy

START = datetime(2024, 1, 1, tzinfo=UTC)

# A market oscillating inside its own band, then one bar that breaks out of it.
CHOP = ["100", "101", "99"] * 10


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
                high=max(previous, close),
                low=min(previous, close),
                close=close,
            )
        )
        previous = close
    return candles


def collect(closes: list[str], strategy: BollingerReversionStrategy) -> dict[int, OrderIntent]:
    candles = series(closes)
    found: dict[int, OrderIntent] = {}
    for index, candle in enumerate(candles):
        intent = strategy.on_candle(candle, candles[: index + 1])
        if intent is not None:
            found[index] = intent
    return found


def test_lower_band_touch_is_a_long() -> None:
    strategy = BollingerReversionStrategy()

    intents = collect([*CHOP, "90"], strategy)

    intent = intents[30]
    assert intent.side is Side.BUY
    assert intent.price == Decimal("90")
    assert intent.stop_loss < intent.price
    assert intent.take_profit is not None and intent.take_profit > intent.price
    assert intent.strategy_id == "bollinger_reversion"
    assert "lower band" in intent.reason


def test_upper_band_touch_is_a_short() -> None:
    strategy = BollingerReversionStrategy()

    intents = collect([*CHOP, "110"], strategy)

    intent = intents[30]
    assert intent.side is Side.SELL
    assert intent.price == Decimal("110")
    assert intent.stop_loss > intent.price
    assert intent.take_profit is not None and intent.take_profit < intent.price
    assert "upper band" in intent.reason


def test_price_inside_the_bands_produces_nothing() -> None:
    assert collect(CHOP, BollingerReversionStrategy()) == {}


def test_no_signal_during_warmup() -> None:
    strategy = BollingerReversionStrategy()

    assert strategy.warmup_bars == strategy.params.period + 1
    assert collect(["90"] * 5, strategy) == {}


def test_bands_are_judged_on_the_configured_window() -> None:
    params = BollingerParams(period=5, num_std=Decimal("1"))
    strategy = BollingerReversionStrategy(params=params)

    intents = collect([*CHOP, "90"], strategy)

    assert intents  # a tighter window still sees the break
    assert min(intents) >= params.period


def test_take_profit_must_clear_the_stop() -> None:
    with pytest.raises(ValidationError, match="take_profit_pct"):
        BollingerParams(stop_loss_pct=Decimal("4"), take_profit_pct=Decimal("2"))


def test_strategy_is_deterministic() -> None:
    first = collect([*CHOP, "90"], BollingerReversionStrategy())
    second = collect([*CHOP, "90"], BollingerReversionStrategy())

    assert first == second
