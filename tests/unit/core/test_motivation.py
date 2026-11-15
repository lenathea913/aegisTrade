"""Motivation quotes: fixed list, random pick, no claims about outcomes."""

from __future__ import annotations

import random

from aegis_core import TRADER_MOTIVATION_QUOTE, get_random_motivation


def test_the_quote_list_is_the_documented_one() -> None:
    assert TRADER_MOTIVATION_QUOTE == (
        "If your backtest fails, start over.",
        "If you lose, change your strategy.",
        "If you drawdown, rise stronger.",
        "If your order gets rejected by RiskEngine, improve yourself.",
        "If you get tired of the charts, rest, but never quit.",
    )


def test_random_pick_returns_a_member() -> None:
    quote = get_random_motivation()

    assert quote in TRADER_MOTIVATION_QUOTE


def test_random_pick_is_injectable_for_determinism() -> None:
    rng = random.Random(7)

    picks = [get_random_motivation(rng) for _ in range(5)]

    assert all(pick in TRADER_MOTIVATION_QUOTE for pick in picks)
    assert len(set(picks)) > 1
