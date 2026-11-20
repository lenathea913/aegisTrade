"""Futures leverage, isolated margin and liquidation in the backtest engine."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_backtest import BacktestConfig, ExitReason, run
from aegis_core import (
    MAX_LEVERAGE,
    Candle,
    OrderIntent,
    OrderType,
    Side,
    Timeframe,
    liquidation_price,
)
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
    stop: str = "50",
    order_type: OrderType = OrderType.MARKET,
) -> OrderIntent:
    return OrderIntent(
        symbol="BTC/USDT",
        side=side,
        order_type=order_type,
        qty=Decimal(qty),
        price=Decimal(price),
        stop_loss=Decimal(stop),
        strategy_id="scripted",
        reason="test signal",
    )


def free(**overrides: object) -> BacktestConfig:
    """No fees and no slippage, so every expected number is exact."""
    values: dict[str, object] = {
        "initial_equity": Decimal("10000"),
        "taker_fee_bps": Decimal("0"),
        "maker_fee_bps": Decimal("0"),
        "slippage_bps": Decimal("0"),
        "allow_short": True,
    }
    values.update(overrides)
    return BacktestConfig(**values)


#: Entry at bar 1 open (100) and a marked close at 110 on bar 2.
UP_THEN_FLAT = [
    ("100", "101", "99", "100"),
    ("100", "102", "99", "100"),
    ("100", "111", "99", "110"),
]


def test_only_isolated_margin_and_the_hard_leverage_cap_are_accepted() -> None:
    assert BacktestConfig().leverage == 1
    assert BacktestConfig().margin_type == "ISOLATED"

    with pytest.raises(ValidationError):
        BacktestConfig(leverage=MAX_LEVERAGE + 1)

    with pytest.raises(ValidationError):
        BacktestConfig(leverage=0)

    with pytest.raises(ValidationError):
        BacktestConfig(margin_type="CROSSED")


def test_three_x_scales_the_pnl_of_the_same_signal() -> None:
    strategy = ScriptedStrategy({0: make_intent()})

    one = run(strategy, series(UP_THEN_FLAT), free())
    three = run(strategy, series(UP_THEN_FLAT), free(leverage=3))

    (one_trade,) = one.trades
    (three_trade,) = three.trades

    assert one_trade.qty == Decimal("1")
    assert three_trade.qty == Decimal("3")  # same margin, three times the notional
    assert one_trade.net_pnl == Decimal("10")
    assert three_trade.net_pnl == Decimal("30")
    assert three.final_equity == Decimal("10000") + Decimal("30")
    assert three.liquidations == 0


def test_margin_locked_is_notional_over_leverage() -> None:
    strategy = ScriptedStrategy({0: make_intent()})

    result = run(strategy, series(UP_THEN_FLAT), free(leverage=3))

    # Bar 1: the 300 notional is backed by 100 of margin, so 9900 stays free and the
    # equity at the unchanged mark is still the initial 10000.
    point = result.equity_curve[1]
    assert point.cash == Decimal("9900")
    assert point.equity == Decimal("10000")
    assert point.position_qty == Decimal("3")


def test_long_is_liquidated_at_the_estimated_price() -> None:
    rows = [
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("100", "101", "60", "65"),  # deep crash, the 50 stop is never reached
    ]
    strategy = ScriptedStrategy({0: make_intent(stop="50")})
    expected = liquidation_price(Decimal("100"), Side.BUY, 3)

    result = run(strategy, series(rows), free(leverage=3))

    (trade,) = result.trades
    assert expected < Decimal("70")
    assert trade.exit_reason is ExitReason.LIQUIDATED
    assert trade.exit_price == expected
    assert result.liquidations == 1
    assert result.total_liquidations == 1
    # The collateral (100) is what a liquidation consumes: 98.5 lost, the 1.5 left is
    # the 0.5 % maintenance allowance of the 300 notional.
    assert trade.net_pnl < Decimal("-98.4")
    assert trade.net_pnl > Decimal("-98.6")


def test_stop_fills_first_when_it_is_tighter_than_the_liquidation_price() -> None:
    rows = [
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("99", "99", "90", "92"),
    ]
    strategy = ScriptedStrategy({0: make_intent(stop="98")})

    result = run(strategy, series(rows), free(leverage=3))

    (trade,) = result.trades
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_price == Decimal("98")
    assert result.liquidations == 0


def test_short_is_liquidated_when_its_stop_is_beyond_the_liquidation_price() -> None:
    rows = [
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("100", "200", "99", "150"),
    ]
    strategy = ScriptedStrategy({0: make_intent(side=Side.SELL, stop="150")})
    expected = liquidation_price(Decimal("100"), Side.SELL, 3)

    result = run(strategy, series(rows), free(leverage=3))

    (trade,) = result.trades
    assert trade.side is Side.SELL
    assert trade.exit_reason is ExitReason.LIQUIDATED
    assert trade.exit_price == expected


def test_leverage_one_is_spot_equivalent_and_never_liquidates() -> None:
    rows = [
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("90", "90", "5", "6"),
    ]
    strategy = ScriptedStrategy({0: make_intent(stop="1")})

    result = run(strategy, series(rows), free())

    (trade,) = result.trades
    assert trade.exit_reason is ExitReason.END_OF_DATA
    assert result.liquidations == 0


def test_margin_utilisation_caps_an_unfundable_notional() -> None:
    """At 5x, 10000 of collateral cannot carry 1000 coins at 100 (100000 notional)."""
    strategy = ScriptedStrategy({0: make_intent(qty="1000")})

    result = run(strategy, series(UP_THEN_FLAT), free(leverage=5))

    (trade,) = result.trades
    assert trade.qty == Decimal("475")  # 10000 * 0.95 * 5 / 100
