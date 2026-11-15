"""Trader motivation quotes.

A rejected order is information, not a verdict on the operator. These quotes exist so
a rejection log line ends with something to do next instead of only a reason code.

They are deliberately about *process* (restart the backtest, change the strategy) and
never about outcomes: nothing here claims an edge, a win rate or a recovery.
"""

from __future__ import annotations

import random

__all__ = ["TRADER_MOTIVATION_QUOTE", "get_random_motivation"]

TRADER_MOTIVATION_QUOTE: tuple[str, ...] = (
    "If your backtest fails, start over.",
    "If you lose, change your strategy.",
    "If you drawdown, rise stronger.",
    "If your order gets rejected by RiskEngine, improve yourself.",
    "If you get tired of the charts, rest, but never quit.",
)


def get_random_motivation(rng: random.Random | None = None) -> str:
    """Return one quote at random (``rng`` injectable for deterministic tests)."""
    generator = rng if rng is not None else random
    return generator.choice(TRADER_MOTIVATION_QUOTE)
