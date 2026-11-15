"""Strategy contract.

A strategy is a pure function of closed candles: it receives the candle that just
closed plus the history up to and including it, and returns an ``OrderIntent`` or
``None``. It cannot reach an exchange, cannot size against equity, and cannot see a
future bar — those constraints are what make backtests comparable to live behaviour.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from decimal import Decimal

from aegis_core import Candle, OrderIntent

__all__ = ["StrategyBase", "ema"]


class StrategyBase(ABC):
    """Base class for every strategy."""

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        """Stable identifier written to intents, trades and the audit trail."""

    @property
    def strategy_version(self) -> str:
        """Version of the rules; recorded on signals and trades."""
        return "1"

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Bars needed before ``on_candle`` can produce a signal."""

    @abstractmethod
    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> OrderIntent | None:
        """Return an intent for the *next* bar, or ``None`` to do nothing.

        Args:
            candle: the bar that just closed; the decision timestamp is its close.
            history: every closed bar up to and including ``candle``. Never contains
                a future bar, and must not be retained/mutated.
        """


def ema(values: Sequence[Decimal], period: int) -> Decimal:
    """Exponential moving average seeded with the SMA of the first ``period`` values.

    Deterministic and dependency-free: the seed makes results reproducible across
    runs and platforms, which matters more here than matching any vendor's variant.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        raise ValueError(f"need at least {period} values to compute EMA, got {len(values)}")

    seed = sum(values[:period], Decimal("0")) / Decimal(period)
    alpha = Decimal(2) / Decimal(period + 1)
    result = seed
    for value in values[period:]:
        result = value * alpha + result * (Decimal(1) - alpha)
    return result
