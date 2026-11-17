"""Leverage maths: margin, liquidation, ROE and the dynamic choice.

The numbers here are the contract between the ledger, the risk gate and the dashboard;
if two of them disagree, one of these tests fails.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aegis_core import (
    MAX_LEVERAGE,
    Side,
    choose_leverage,
    clamp_leverage,
    liquidation_price,
    margin_for,
    roe_pct,
)


def test_margin_is_notional_over_leverage() -> None:
    assert margin_for(Decimal("3000"), 3) == Decimal("1000")
    assert margin_for(Decimal("3000"), 1) == Decimal("3000")
    assert margin_for(Decimal("0"), 5) == Decimal("0")


def test_liquidation_of_a_long_sits_below_the_entry() -> None:
    # 5x: 1 - 1/5 + 0.005 = 0.805
    assert liquidation_price(Decimal("30000"), Side.BUY, 5) == Decimal("24150.000")


def test_liquidation_of_a_short_sits_above_the_entry() -> None:
    # 5x: 1 + 1/5 - 0.005 = 1.195
    assert liquidation_price(Decimal("30000"), Side.SELL, 5) == Decimal("35850.000")


def test_one_x_only_liquidates_at_the_maintenance_requirement() -> None:
    """At 1x the collateral equals the notional, so only a 99.5 % drop is a liquidation."""
    assert liquidation_price(Decimal("30000"), Side.BUY, 1) == Decimal("150.000")
    assert liquidation_price(Decimal("30000"), Side.SELL, 1) == Decimal("59850.000")


def test_roe_is_pnl_over_posted_margin() -> None:
    assert roe_pct(Decimal("100"), Decimal("1000")) == Decimal("10")
    assert roe_pct(Decimal("-50"), Decimal("1000")) == Decimal("-5")
    assert roe_pct(Decimal("10"), Decimal("0")) == Decimal("0")


def test_more_leverage_shows_more_roe_for_the_same_move() -> None:
    entry = Decimal("30000")
    mark = Decimal("30300")
    pnl = (mark - entry) * Decimal("1")

    three = roe_pct(pnl, margin_for(entry, 3))
    five = roe_pct(pnl, margin_for(entry, 5))

    assert pnl == Decimal("300")
    assert five > three


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 1), (1, 1), (3, 3), (5, 5), (9, 5), (-2, 1)],
)
def test_clamp_never_exceeds_the_hard_cap(value: int, expected: int) -> None:
    assert clamp_leverage(value) == expected


def test_clamp_respects_a_tighter_configured_cap() -> None:
    assert clamp_leverage(4, maximum=2) == 2
    assert clamp_leverage(1, maximum=3) == 1


def test_a_wide_stop_needs_no_leverage() -> None:
    """2 % away: notional 5 000 on 10 000 equity -> 0.5x, rounded up to 1x."""
    assert (
        choose_leverage(
            equity=Decimal("10000"),
            entry=Decimal("30000"),
            stop_loss=Decimal("29400"),
            risk_pct=Decimal("1"),
        )
        == 1
    )


def test_a_tight_stop_needs_and_gets_more_leverage() -> None:
    """0.4 % away: notional 25 000 on 10 000 equity -> 2.5x -> 3x."""
    assert (
        choose_leverage(
            equity=Decimal("10000"),
            entry=Decimal("30000"),
            stop_loss=Decimal("29880"),
            risk_pct=Decimal("1"),
        )
        == 3
    )


def test_dynamic_leverage_is_capped_by_both_limits() -> None:
    tight = choose_leverage(
        equity=Decimal("10000"),
        entry=Decimal("30000"),
        stop_loss=Decimal("29999"),  # 0.003 %: would want ~3000x
        risk_pct=Decimal("1"),
    )
    tighter_config = choose_leverage(
        equity=Decimal("10000"),
        entry=Decimal("30000"),
        stop_loss=Decimal("29999"),
        risk_pct=Decimal("1"),
        max_leverage=2,
    )

    assert tight == MAX_LEVERAGE == 5
    assert tighter_config == 2


def test_degenerate_inputs_fall_back_to_one_x() -> None:
    assert (
        choose_leverage(
            equity=Decimal("0"), entry=Decimal("1"), stop_loss=Decimal("1"), risk_pct=Decimal("1")
        )
        == 1
    )
    assert (
        choose_leverage(
            equity=Decimal("10000"),
            entry=Decimal("30000"),
            stop_loss=Decimal("30000"),
            risk_pct=Decimal("1"),
        )
        == 1
    )
