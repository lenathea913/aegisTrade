"""Leverage maths, in one place, with the platform's hard cap.

Futures trading adds three numbers that every layer needs and that must agree:
margin, liquidation price and ROE. Deriving them here keeps the ledger, the risk gate
and the dashboard from re-inventing the formula (and disagreeing at the fourth decimal).

Conventions:

* ``leverage`` is an integer multiplier, 1 (spot-equivalent) to :data:`MAX_LEVERAGE`.
* Margin is *isolated*: the position carries its own collateral,
  ``notional / leverage``, and nothing else can draw on it.
* PnL on a linear contract is independent of leverage — only the *return on the
  posted margin* (ROE) scales. That distinction is why a 5x position shows a 5x
  larger ROE for the same price move.
* The liquidation price is the simplified isolated-margin formula
  ``entry * (1 -/+ 1/leverage +/- maintenance_margin_rate)``. It is an estimate for
  display and for a sanity check (never a substitute for the venue's own number), so
  it deliberately ignores fees and the exact maintenance-margin tiers.
"""

from __future__ import annotations

import math
from decimal import Decimal

from aegis_core.enums import Side

__all__ = [
    "MAINTENANCE_MARGIN_PCT",
    "MARGIN_UTILISATION",
    "MAX_LEVERAGE",
    "choose_leverage",
    "clamp_leverage",
    "liquidation_price",
    "margin_for",
    "roe_pct",
]

#: Hard ceiling. Risk configuration cannot raise it, and neither can a strategy: the
#: whole point of the cap is that no single typo can turn a 2 % stop into an account
#: liquidation.
MAX_LEVERAGE = 5

#: Maintenance-margin allowance used by the liquidation estimate (0.5 %).
MAINTENANCE_MARGIN_PCT = Decimal("0.5")

#: Fraction of ``equity * leverage`` a position may consume, leaving headroom for fees,
#: slippage and a funding tick between sizing and the fill.
MARGIN_UTILISATION = Decimal("0.95")

_HUNDRED = Decimal("100")
_ONE = Decimal("1")


def clamp_leverage(value: int | Decimal, *, maximum: int = MAX_LEVERAGE) -> int:
    """Coerce to an integer in ``[1, min(maximum, MAX_LEVERAGE)]``.

    ``maximum`` is the *requested* cap (configuration/risk limits); the hard
    :data:`MAX_LEVERAGE` always wins.
    """
    cap = max(1, min(int(maximum), MAX_LEVERAGE))
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = 1
    return max(1, min(requested, cap))


def margin_for(notional: Decimal, leverage: int) -> Decimal:
    """Isolated collateral behind ``notional`` at ``leverage``."""
    if leverage < 1:
        raise ValueError("leverage must be at least 1")
    if notional <= 0:
        return Decimal("0")
    return notional / Decimal(clamp_leverage(leverage))


def liquidation_price(
    entry: Decimal,
    side: Side,
    leverage: int,
    *,
    maintenance_margin_pct: Decimal = MAINTENANCE_MARGIN_PCT,
) -> Decimal:
    """Estimated isolated-margin liquidation price for one position.

    At 1x the collateral equals the notional, so the estimate falls to the
    maintenance-margin requirement (``entry * 0.005`` for a long) rather than zero:
    a 99.5 % drop would still be called. Callers that display it for a spot position
    should show nothing, because spot has no liquidation at all.
    """
    if entry <= 0:
        raise ValueError("entry price must be greater than 0")
    multiple = Decimal(clamp_leverage(leverage))
    maintenance = maintenance_margin_pct / _HUNDRED
    if side is Side.BUY:
        price = entry * (_ONE - _ONE / multiple + maintenance)
    else:
        price = entry * (_ONE + _ONE / multiple - maintenance)
    return max(price, Decimal("0"))


def roe_pct(unrealized_pnl: Decimal, margin: Decimal) -> Decimal:
    """Return on margin in percent units (``5`` == +5 %). Zero margin means zero ROE."""
    if margin <= 0:
        return Decimal("0")
    return unrealized_pnl / margin * _HUNDRED


def choose_leverage(
    *,
    equity: Decimal,
    entry: Decimal,
    stop_loss: Decimal,
    risk_pct: Decimal,
    max_leverage: int = MAX_LEVERAGE,
) -> int:
    """Smallest leverage (1..cap) that can hold the fixed-fractional position.

    The sizing rule is fixed-fractional: risk ``risk_pct`` of equity if the stop is
    hit, so ``qty = equity * risk_pct / |entry - stop|``. That notional may exceed the
    cash available, and this is where leverage earns its place:

        required = notional / equity

    A wide stop (high volatility) sizes small and needs 1x; a tight stop sizes large
    and needs more. Rounding *up* to the next integer means the position always fits
    inside its margin, and the result is clamped so neither configuration nor a wild
    stop distance can ask for more than the cap.
    """
    if equity <= 0 or entry <= 0:
        return 1
    distance = abs(entry - stop_loss)
    if distance <= 0:
        return 1
    notional = (equity * risk_pct / _HUNDRED) / distance * entry
    required = notional / equity
    try:
        steps = math.ceil(required)
    except (OverflowError, ValueError):  # pragma: no cover - defensive
        return clamp_leverage(1, maximum=max_leverage)
    return clamp_leverage(steps, maximum=max_leverage)
