"""Backtest engine: fill timing, costs, stops, funding, fills and validation.

Prices are written as strings and comparisons use exact ``Decimal`` equality, so a
fee that is off by one basis point fails the test.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aegis_backtest import BacktestConfig, ExitReason, run
from aegis_core import Candle, OrderIntent, OrderType, Side, Timeframe
from aegis_strategy import StrategyBase

START = datetime(2024, 1, 1, tzinfo=UTC)


def candle(index: int, o: str, h: str, low_px: str, c: str) -> Candle:
    open_time = START + timedelta(hours=index)
    return Candle(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        open_time=open_time,
        close_time=open_time + timedelta(hours=1),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low_px),
        close=Decimal(c),
    )


def series(rows: list[tuple[str, str, str, str]]) -> list[Candle]:
    return [candle(index, *row) for index, row in enumerate(rows)]


class ScriptedStrategy(StrategyBase):
    """Emits fixed intents at fixed bar indices (after that bar closes)."""

    def __init__(self, intents: dict[int, OrderIntent]) -> None:
        self._intents = intents

    @property
    def strategy_id(self) -> str:
        return "scripted"

    @property
    def warmup_bars(self) -> int:
        return 0

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> OrderIntent | None:
        template = self._intents.get(len(history) - 1)
        if template is None:
            return None
        return template.model_copy(update={"created_at": candle.close_time})


def make_intent(
    *,
    side: Side = Side.BUY,
    qty: str = "1",
    price: str = "100",
    stop: str = "90",
    take_profit: str | None = None,
    order_type: OrderType = OrderType.MARKET,
) -> OrderIntent:
    return OrderIntent(
        symbol="BTC/USDT",
        side=side,
        order_type=order_type,
        qty=Decimal(qty),
        price=Decimal(price),
        stop_loss=Decimal(stop),
        take_profit=None if take_profit is None else Decimal(take_profit),
        strategy_id="scripted",
        reason="test signal",
    )


FREE = BacktestConfig(
    initial_equity=Decimal("10000"),
    taker_fee_bps=Decimal("10"),
    maker_fee_bps=Decimal("0"),
    slippage_bps=Decimal("0"),
)

# bar 0: signal here (close 100) -> bar 1: fill at open 110 -> bar 2: exit at open 111
THREE_BARS = [
    ("100", "101", "99", "100"),
    ("110", "112", "105", "111"),
    ("111", "112", "108", "109"),
]


def test_signal_never_fills_on_its_own_bar() -> None:
    data = series([("100", "101", "99", "100"), ("101", "102", "100", "101")])
    strategy = ScriptedStrategy({1: make_intent()})  # signal on the last bar

    result = run(strategy, data, FREE)

    assert result.trades == ()
    assert result.final_equity == Decimal("10000")


def test_fill_happens_at_next_bar_open_with_taker_fee() -> None:
    strategy = ScriptedStrategy({0: make_intent(), 1: make_intent(side=Side.SELL, stop="120")})

    result = run(strategy, data=series(THREE_BARS), config=FREE)

    (trade,) = result.trades
    assert trade.side is Side.BUY
    assert trade.entry_price == Decimal("110")
    assert trade.exit_price == Decimal("111")
    assert trade.exit_reason is ExitReason.SIGNAL
    assert trade.entry_time == START + timedelta(hours=1)
    assert trade.exit_time == START + timedelta(hours=2)
    assert trade.fee == Decimal("0.11") + Decimal("0.111")
    assert trade.net_pnl == Decimal("0.779")
    assert result.final_equity == Decimal("10000.779")


def test_slippage_is_adverse_on_both_sides() -> None:
    strategy = ScriptedStrategy({0: make_intent(), 1: make_intent(side=Side.SELL, stop="120")})
    config = BacktestConfig(
        initial_equity=Decimal("10000"),
        taker_fee_bps=Decimal("10"),
        maker_fee_bps=Decimal("0"),
        slippage_bps=Decimal("10"),
    )

    result = run(strategy, data=series(THREE_BARS), config=config)

    (trade,) = result.trades
    entry = Decimal("110") * Decimal("1.001")
    exit_price = Decimal("111") * Decimal("0.999")
    expected_net = (exit_price - entry) - entry * Decimal("0.001") - exit_price * Decimal("0.001")

    assert trade.entry_price == entry
    assert trade.exit_price == exit_price
    assert trade.net_pnl == expected_net


def test_stop_loss_exits_intrabar_at_the_stop() -> None:
    data = series(
        [
            ("100", "101", "99", "100"),
            ("100", "101", "97", "99"),  # low trades through the 98 stop
            ("99", "100", "98", "99"),
        ]
    )
    strategy = ScriptedStrategy({0: make_intent(stop="98")})

    result = run(strategy, data, FREE)

    (trade,) = result.trades
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_price == Decimal("98")
    assert trade.net_pnl == Decimal("-2") - Decimal("0.1") - Decimal("0.098")


def test_take_profit_exits_intrabar_at_the_target() -> None:
    data = series(
        [
            ("100", "101", "99", "100"),
            ("100", "106", "99", "104"),
            ("104", "105", "103", "104"),
        ]
    )
    strategy = ScriptedStrategy({0: make_intent(stop="98", take_profit="105")})

    result = run(strategy, data, FREE)

    (trade,) = result.trades
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert trade.exit_price == Decimal("105")


def test_stop_wins_when_both_levels_are_crossed_in_one_bar() -> None:
    data = series(
        [
            ("100", "101", "99", "100"),
            ("100", "106", "97", "104"),  # touches both 98 stop and 105 target
            ("104", "105", "103", "104"),
        ]
    )
    strategy = ScriptedStrategy({0: make_intent(stop="98", take_profit="105")})

    result = run(strategy, data, FREE)

    (trade,) = result.trades
    assert trade.exit_reason is ExitReason.STOP_LOSS


def test_latency_delays_the_fill() -> None:
    strategy = ScriptedStrategy({0: make_intent(stop="50")})
    config = BacktestConfig(
        initial_equity=Decimal("10000"),
        taker_fee_bps=Decimal("10"),
        maker_fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        latency_bars=1,
    )

    result = run(strategy, data=series(THREE_BARS), config=config)

    (trade,) = result.trades
    assert trade.entry_price == Decimal("111")  # bar 2 open, not bar 1
    assert trade.entry_time == START + timedelta(hours=2)


def test_limit_order_fills_at_limit_with_maker_fee() -> None:
    data = series(
        [
            ("100", "101", "99", "100"),
            ("99", "103", "98", "101"),
        ]
    )
    strategy = ScriptedStrategy(
        {0: make_intent(order_type=OrderType.LIMIT, price="100", stop="90")}
    )
    config = BacktestConfig(
        initial_equity=Decimal("10000"),
        taker_fee_bps=Decimal("10"),
        maker_fee_bps=Decimal("2"),
        slippage_bps=Decimal("0"),
    )

    result = run(strategy, data, config)

    (trade,) = result.trades
    assert trade.entry_price == Decimal("100")
    assert trade.exit_reason is ExitReason.END_OF_DATA
    assert trade.fee == Decimal("0.02") + Decimal("0.101")  # maker in, taker out


def test_limit_order_is_dropped_when_untouched() -> None:
    data = series(
        [
            ("100", "101", "99", "100"),
            ("101", "103", "101", "102"),  # never trades down to 100
        ]
    )
    strategy = ScriptedStrategy(
        {0: make_intent(order_type=OrderType.LIMIT, price="100", stop="90")}
    )

    result = run(strategy, data, FREE)

    assert result.trades == ()
    assert result.final_equity == Decimal("10000")


def test_partial_fill_reduces_the_quantity() -> None:
    strategy = ScriptedStrategy({0: make_intent(stop="50")})
    config = BacktestConfig(
        initial_equity=Decimal("10000"),
        taker_fee_bps=Decimal("10"),
        slippage_bps=Decimal("0"),
        partial_fill_ratio=Decimal("0.5"),
    )

    result = run(strategy, data=series(THREE_BARS), config=config)

    (trade,) = result.trades
    assert trade.qty == Decimal("0.5")
    assert trade.fee == Decimal("0.055") + Decimal("0.0545")


def test_funding_is_charged_on_8h_boundaries() -> None:
    flat = [("100", "100.5", "99.5", "100")] * 10  # 00:00 .. 09:00, flat prices
    strategy = ScriptedStrategy({0: make_intent(stop="50")})
    config = BacktestConfig(
        initial_equity=Decimal("10000"),
        taker_fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        funding_rate_bps_per_8h=Decimal("10"),
    )

    result = run(strategy, data=series(flat), config=config)

    (trade,) = result.trades
    assert trade.funding == Decimal("0.1")  # 1 * 100 * 10/10000, charged at 08:00
    assert trade.net_pnl == Decimal("-0.1")


def test_no_funding_charge_without_a_position_or_rate() -> None:
    flat = [("100", "100.5", "99.5", "100")] * 10
    strategy = ScriptedStrategy({0: make_intent(stop="50")})

    result = run(strategy, data=series(flat), config=FREE)

    (trade,) = result.trades
    assert trade.funding == 0


def test_sell_signal_does_not_short_by_default() -> None:
    strategy = ScriptedStrategy({0: make_intent(side=Side.SELL, stop="120")})

    result = run(strategy, data=series(THREE_BARS), config=FREE)

    assert result.trades == ()


def test_short_is_allowed_only_when_enabled() -> None:
    strategy = ScriptedStrategy({0: make_intent(side=Side.SELL, stop="120")})
    config = BacktestConfig(
        initial_equity=Decimal("10000"),
        taker_fee_bps=Decimal("10"),
        slippage_bps=Decimal("0"),
        allow_short=True,
    )

    result = run(strategy, data=series(THREE_BARS), config=config)

    (trade,) = result.trades
    assert trade.side is Side.SELL
    assert trade.entry_price == Decimal("110")
    assert trade.exit_price == Decimal("109")  # end-of-data close


def test_opposite_signal_closes_without_flipping() -> None:
    strategy = ScriptedStrategy(
        {0: make_intent(), 1: make_intent(side=Side.SELL, stop="120", qty="2")}
    )

    result = run(strategy, data=series(THREE_BARS), config=FREE)

    (trade,) = result.trades
    assert trade.side is Side.BUY
    assert trade.exit_reason is ExitReason.SIGNAL


def test_same_direction_signal_does_not_pyramid() -> None:
    strategy = ScriptedStrategy({0: make_intent(), 1: make_intent(qty="5")})

    result = run(strategy, data=series(THREE_BARS), config=FREE)

    (trade,) = result.trades
    assert trade.qty == Decimal("1")
    assert trade.exit_reason is ExitReason.END_OF_DATA


def test_opposite_signal_flips_when_shorting_is_allowed() -> None:
    strategy = ScriptedStrategy(
        {0: make_intent(), 1: make_intent(side=Side.SELL, stop="120", qty="1")}
    )
    config = BacktestConfig(
        initial_equity=Decimal("10000"),
        taker_fee_bps=Decimal("10"),
        slippage_bps=Decimal("0"),
        allow_short=True,
    )

    result = run(strategy, data=series(THREE_BARS), config=config)

    assert len(result.trades) == 2
    long_trade, short_trade = result.trades
    assert long_trade.side is Side.BUY
    assert long_trade.exit_reason is ExitReason.SIGNAL
    assert short_trade.side is Side.SELL
    assert short_trade.entry_price == Decimal("111")
    assert short_trade.exit_reason is ExitReason.END_OF_DATA


def test_short_stop_loss_triggers_on_a_rally() -> None:
    data = series(
        [
            ("100", "101", "99", "100"),
            ("100", "102", "99", "101"),
            ("101", "106", "100", "105"),  # high trades through the 105 stop
        ]
    )
    strategy = ScriptedStrategy({0: make_intent(side=Side.SELL, stop="105")})
    config = BacktestConfig(
        initial_equity=Decimal("10000"),
        taker_fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        allow_short=True,
    )

    result = run(strategy, data, config)

    (trade,) = result.trades
    assert trade.side is Side.SELL
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_price == Decimal("105")
    assert trade.net_pnl == Decimal("-5")


def test_short_take_profit_triggers_on_a_drop() -> None:
    data = series(
        [
            ("100", "101", "99", "100"),
            ("100", "101", "99", "100"),
            ("100", "101", "94", "96"),  # low trades through the 95 target
        ]
    )
    strategy = ScriptedStrategy({0: make_intent(side=Side.SELL, stop="110", take_profit="95")})
    config = BacktestConfig(
        initial_equity=Decimal("10000"),
        taker_fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        allow_short=True,
    )

    result = run(strategy, data, config)

    (trade,) = result.trades
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert trade.exit_price == Decimal("95")
    assert trade.net_pnl == Decimal("5")


def test_mixed_timeframes_are_rejected() -> None:
    data = series(THREE_BARS)
    data[1] = data[1].model_copy(update={"timeframe": Timeframe.H4})

    with pytest.raises(ValueError, match="one timeframe"):
        run(ScriptedStrategy({}), data, FREE)


def test_open_position_can_be_left_open() -> None:
    strategy = ScriptedStrategy({0: make_intent(stop="50")})
    config = BacktestConfig(
        initial_equity=Decimal("10000"),
        taker_fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        close_at_end=False,
    )

    result = run(strategy, data=series(THREE_BARS), config=config)

    assert result.trades == ()
    assert result.equity_curve[-1].position_qty == Decimal("1")


def test_equity_curve_covers_every_bar() -> None:
    strategy = ScriptedStrategy({})
    data = series(THREE_BARS)

    result = run(strategy, data, FREE)

    assert len(result.equity_curve) == len(data)
    assert [point.timestamp for point in result.equity_curve] == [bar.close_time for bar in data]


def test_final_equity_equals_initial_plus_realized_pnl() -> None:
    data = series(
        [
            ("100", "101", "99", "100"),
            ("100", "101", "97", "99"),
            ("99", "104", "98", "103"),
            ("103", "104", "101", "102"),
            ("102", "110", "101", "109"),
        ]
    )
    strategy = ScriptedStrategy(
        {
            0: make_intent(stop="98", take_profit="1000"),
            1: make_intent(side=Side.SELL, stop="120"),
            3: make_intent(stop="90"),
        }
    )

    result = run(strategy, data, FREE)

    realized = sum((trade.net_pnl for trade in result.trades), Decimal("0"))
    assert result.final_equity == Decimal("10000") + realized


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "at least one candle"),
        (
            [candle(0, "1", "1", "1", "1"), candle(0, "1", "1", "1", "1")],
            "strictly increasing",
        ),
    ],
)
def test_invalid_series_is_rejected(data: list[Candle], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        run(ScriptedStrategy({}), data, FREE)


def test_mixed_symbols_are_rejected() -> None:
    data = series(THREE_BARS)
    data[1] = data[1].model_copy(update={"symbol": "ETH/USDT"})

    with pytest.raises(ValueError, match="one symbol"):
        run(ScriptedStrategy({}), data, FREE)


def test_empty_strategy_produces_a_flat_curve() -> None:
    result = run(ScriptedStrategy({}), series(THREE_BARS), FREE)

    assert result.metrics.total_trades == 0
    assert result.final_equity == Decimal("10000")
    assert result.metrics.max_drawdown == 0.0
