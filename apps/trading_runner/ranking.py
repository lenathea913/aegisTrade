"""Which of several simultaneous signals gets the risk engine's few slots.

``max_open_positions`` turns every scan into a contest: ten symbols can each produce a
``RISK_OK`` cross in the same hour, but only three can be entered. The order the
candidates are offered in *is* the selection rule, so it has to measure the idea and
not the ticker.

Reward:risk cannot do that job here. The demo strategy fixes the stop at 2 % and the
target at 4 %, so every signal reports ``risk_reward == 2`` and sorting by it silently
degenerated into alphabetical order — ``DOGE`` beat ``BTC`` because ``D`` sorts first.

This module scores a signal by how strong the move behind it is:

* **ADX** — the trend's directional strength. 25 is the strategy's own entry floor,
  40+ is a market that is going somewhere; above that a cross is more likely to
  continue than to mean-revert.
* **distance beyond the slow EMA** — how far price has already travelled from the
  average it just crossed. A cross that immediately stalls sits at ~0 %; a cross that
  carries sits further away, which is the momentum the strategy is trying to ride.

The two are added with ADX in index points and distance scaled by
``DISTANCE_WEIGHT`` (10 points per 1 %), so a 1 % move beyond the EMA is worth ten ADX
points. Both are recorded on the signal by ``SignalService`` at generation time; a
signal from before this metric existed scores 0 and simply sorts last, never crashes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from aegis_core import Signal

__all__ = [
    "ADX_WEIGHT",
    "DISTANCE_WEIGHT",
    "rank_candidates",
    "signal_strength",
    "strength_from_context",
]

#: ADX is already on a 0-100 scale, so its own points are the score contribution.
ADX_WEIGHT = Decimal("1")
#: One percent beyond the slow EMA counts as ten ADX points.
DISTANCE_WEIGHT = Decimal("10")

_ZERO = Decimal("0")


def _metric(context: Mapping[str, Any], key: str) -> Decimal:
    """Read one numeric metric, treating a missing or malformed value as zero."""
    raw = context.get(key)
    if raw is None or isinstance(raw, bool):
        return _ZERO
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return _ZERO


def strength_from_context(context: Mapping[str, Any]) -> Decimal:
    """Signal strength from the signal's ``market_context`` metrics.

    ``adx`` and ``ema_distance_pct`` are written by ``SignalService`` when the signal
    is generated; anything unreadable contributes 0.
    """
    adx_component = _metric(context, "adx") * ADX_WEIGHT
    distance_component = _metric(context, "ema_distance_pct") * DISTANCE_WEIGHT
    return adx_component + distance_component


def signal_strength(signal: Signal) -> Decimal:
    """How strong the move behind ``signal`` is; higher is better."""
    return strength_from_context(signal.market_context)


def rank_candidates(signals: Iterable[Signal]) -> tuple[Signal, ...]:
    """Best idea first, strongest trend first.

    The symbol is only a tie-break, so two equally strong signals still produce a
    stable, reproducible order. It is not the ranking: a weaker signal never wins
    because its ticker happens to come first alphabetically.
    """
    return tuple(sorted(signals, key=lambda signal: (-signal_strength(signal), signal.symbol)))
