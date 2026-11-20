"""EMA helper and the demo EMA-cross strategy (signals only, never execution)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import Candle, OrderIntent, OrderType, Side, Timeframe
from aegis_strategy import EmaCrossParams, EmaCrossStrategy, ema

START = datetime(2024, 1, 1, tzinfo=UTC)


def make_series(closes: list[str]) -> list[Candle]:
    """Build a candle per close: open = previous close, high/low bracket the bar."""
    candles: list[Candle] = []
    previous = Decimal(closes[0])
    for index, raw in enumerate(closes):
        close = Decimal(raw)
        open_price = previous
        candles.append(
            Candle(
                symbol="BTC/USDT",
                timeframe=Timeframe.H1,
                open_time=START + timedelta(hours=index),
                close_time=START + timedelta(hours=index + 1),
                open=open_price,
                high=max(open_price, close) + Decimal("1"),
                low=min(open_price, close) - Decimal("1"),
                close=close,
            )
        )
        previous = close
    return candles


# 5 flat bars, a pop up, then two drops that take the fast EMA back below the slow.
CROSS_CLOSES = ["100"] * 5 + ["101", "80", "80"]


def collect_intents(closes: list[str], strategy: EmaCrossStrategy) -> dict[int, OrderIntent]:
    series = make_series(closes)
    intents: dict[int, OrderIntent] = {}
    for index, candle in enumerate(series):
        intent = strategy.on_candle(candle, series[: index + 1])
        if intent is not None:
            intents[index] = intent
    return intents


# ------------------------------------------------------------------------ ema


def test_ema_with_period_one_is_the_last_value() -> None:
    assert ema([Decimal("1"), Decimal("2"), Decimal("3")], 1) == Decimal("3")


def test_ema_seeds_with_the_simple_average() -> None:
    assert ema([Decimal("1"), Decimal("2"), Decimal("3")], 3) == Decimal("2")


def test_ema_needs_enough_values() -> None:
    with pytest.raises(ValueError, match="at least"):
        ema([Decimal("1")], 3)
    with pytest.raises(ValueError, match="period"):
        ema([Decimal("1"), Decimal("2")], 0)


def test_ema_is_deterministic() -> None:
    values = [Decimal(value) for value in ("10", "11", "12", "13", "14")]
    assert ema(values, 3) == ema(values, 3)


# --------------------------------------------------------------------- params


def test_fast_period_must_be_shorter_than_slow() -> None:
    with pytest.raises(ValidationError, match="fast_period"):
        EmaCrossParams(fast_period=26, slow_period=12)


@pytest.mark.parametrize("stop", ["0", "100"])
def test_stop_loss_pct_is_bounded(stop: str) -> None:
    with pytest.raises(ValidationError):
        EmaCrossParams(stop_loss_pct=Decimal(stop))


def test_take_profit_pct_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        EmaCrossParams(take_profit_pct=Decimal("0"))


# ------------------------------------------------------------------- strategy


def test_no_signal_during_warmup() -> None:
    strategy = EmaCrossStrategy(params=EmaCrossParams(fast_period=2, slow_period=4))
    series = make_series(CROSS_CLOSES)

    assert strategy.warmup_bars == 5
    for index in range(4):
        assert strategy.on_candle(series[index], series[: index + 1]) is None


def test_buy_on_up_cross() -> None:
    strategy = EmaCrossStrategy(params=EmaCrossParams(fast_period=2, slow_period=4))

    intents = collect_intents(CROSS_CLOSES, strategy)

    assert 5 in intents
    buy = intents[5]
    assert buy.side is Side.BUY
    assert buy.order_type is OrderType.MARKET
    assert buy.price == Decimal("101")
    assert buy.stop_loss < buy.price
    assert buy.take_profit is not None and buy.take_profit > buy.price
    assert buy.created_at == make_series(CROSS_CLOSES)[5].close_time
    assert buy.strategy_id == "simple_ema_cross"
    assert "crossed above" in buy.reason


def test_sell_on_down_cross() -> None:
    strategy = EmaCrossStrategy(params=EmaCrossParams(fast_period=2, slow_period=4))

    intents = collect_intents(CROSS_CLOSES, strategy)

    assert 6 in intents
    sell = intents[6]
    assert sell.side is Side.SELL
    assert sell.price is not None
    assert sell.stop_loss > sell.price
    assert sell.take_profit is not None and sell.take_profit < sell.price
    assert "crossed below" in sell.reason


def test_flat_market_produces_no_signals() -> None:
    strategy = EmaCrossStrategy(params=EmaCrossParams(fast_period=2, slow_period=4))

    assert collect_intents(["100"] * 10, strategy) == {}


def test_other_symbols_are_ignored() -> None:
    strategy = EmaCrossStrategy(
        symbol="BTC/USDT", params=EmaCrossParams(fast_period=2, slow_period=4)
    )
    series = make_series(CROSS_CLOSES)
    foreign = series[5].model_copy(update={"symbol": "ETH/USDT"})

    assert strategy.on_candle(foreign, series[:6]) is None


def test_params_drive_quantity_and_levels() -> None:
    params = EmaCrossParams(
        fast_period=2,
        slow_period=4,
        qty=Decimal("0.25"),
        stop_loss_pct=Decimal("1"),
        take_profit_pct=Decimal("3"),
    )
    strategy = EmaCrossStrategy(params=params)

    intents = collect_intents(CROSS_CLOSES, strategy)
    buy = intents[5]

    assert buy.qty == Decimal("0.25")
    assert buy.stop_loss == Decimal("101") * Decimal("0.99")
    assert buy.take_profit == Decimal("101") * Decimal("1.03")


def test_strategy_is_deterministic() -> None:
    params = EmaCrossParams(fast_period=2, slow_period=4)
    first = collect_intents(CROSS_CLOSES, EmaCrossStrategy(params=params))
    second = collect_intents(CROSS_CLOSES, EmaCrossStrategy(params=params))

    assert first == second


# ------------------------------------------------------------- adx filter

# A four-bar cycle around 100: the fast EMA crosses constantly while price goes
# nowhere, which is exactly the chop the filter is supposed to veto.
CHOP = ["100", "102", "99", "101"] * 80


def test_adx_filter_removes_most_crosses_in_a_choppy_market() -> None:
    unfiltered = collect_intents(
        CHOP,
        EmaCrossStrategy(params=EmaCrossParams(fast_period=2, slow_period=4, adx_threshold=None)),
    )
    filtered = collect_intents(
        CHOP,
        EmaCrossStrategy(
            params=EmaCrossParams(fast_period=2, slow_period=4, adx_threshold=Decimal("25"))
        ),
    )

    assert unfiltered, "the fixture must contain crosses for the filter to matter"
    # ADX dips and spikes even inside chop, so a few crossings survive; the point is
    # that the overwhelming majority of them never reach the pipeline.
    assert len(filtered) * 5 < len(unfiltered)


def test_adx_filter_passes_when_the_floor_is_zero() -> None:
    unfiltered = collect_intents(
        CHOP,
        EmaCrossStrategy(params=EmaCrossParams(fast_period=2, slow_period=4, adx_threshold=None)),
    )
    permissive = collect_intents(
        CHOP,
        EmaCrossStrategy(
            params=EmaCrossParams(fast_period=2, slow_period=4, adx_threshold=Decimal("0"))
        ),
    )

    assert permissive == unfiltered


def test_adx_filter_allows_a_cross_inside_a_strong_trend() -> None:
    # A long decline then a recovery: the EMA cross happens while ADX is high.
    closes = [str(100)] * 3
    price = Decimal("100")
    for index in range(300):
        price = price * (Decimal("0.99") if index < 200 else Decimal("1.01"))
        closes.append(str(price))

    intents = collect_intents(
        closes,
        EmaCrossStrategy(params=EmaCrossParams(fast_period=9, slow_period=21)),
    )

    assert intents, "a strong trend should clear the ADX floor"


def test_symbol_is_normalised() -> None:
    strategy = EmaCrossStrategy(symbol="btc-usdt")

    assert strategy.symbol == "BTC/USDT"
    assert strategy.strategy_id == "simple_ema_cross"
    assert strategy.timeframe is Timeframe.H1
