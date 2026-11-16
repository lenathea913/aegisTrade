"""Number formatting for the dashboard.

The ledger and the audit trail hold exact ``Decimal`` values, so a raw amount can
arrive at the template as ``9999.09955000000000010018009``. Rounding is a presentation
concern and happens here, at the very edge:

* ``money`` — fiat amounts (equity, PnL, notional, budgets): always 2 decimals.
* ``price`` — instrument prices, at the venue's tick precision (``0.01`` -> 2 decimals
  for BTC/USDT, ``0.000001`` -> 6 for SHIB/USDT). Precision comes from
  ``api.instruments.REGISTRY``, primed from ``exchangeInfo`` at startup; a symbol the
  venue has not described yet falls back to two decimals.
* ``qty`` — quantities, at the venue's lot step, trailing zeros removed (``0.06``).
* ``fee`` — fees keep at least six decimals, so ``0.004502`` never renders as ``0.00``.
* ``clean_payload`` — the same rules applied recursively to a JSON payload, keyed by
  field name rather than guessed from magnitude.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from api.instruments import REGISTRY

__all__ = ["clean_payload", "fee", "money", "price", "qty"]

_MONEY_STEP = Decimal("0.01")
_MIN_FEE_DECIMALS = 6
_MAX_FEE_DECIMALS = 8
_FEE_KEYS = frozenset({"fee", "funding"})
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _step(decimals: int) -> Decimal:
    return Decimal(1).scaleb(-decimals)


def _fixed(value: Any, step: Decimal, rounding: str = ROUND_HALF_UP) -> str:
    number = _as_decimal(value)
    if number is None:
        return "" if value is None else str(value)
    return format(number.quantize(step, rounding=rounding), "f")


def money(value: Any) -> str:
    """Fiat-style amount with exactly two decimals (``9999.0995...`` -> ``9999.10``)."""
    return _fixed(value, _MONEY_STEP)


def price(value: Any, symbol: str | None = None) -> str:
    """Instrument price at the venue's tick precision (``BTC/USDT`` -> 2 decimals)."""
    return _fixed(value, _step(REGISTRY.price_decimals(symbol)))


def qty(value: Any, symbol: str | None = None) -> str:
    """Quantity at the venue's lot step, trailing zeros removed (``0.0600`` -> ``0.06``)."""
    # Truncated, not rounded: a quantity has to stay executable at the venue step.
    text = _fixed(value, _step(REGISTRY.qty_decimals(symbol)), ROUND_DOWN)
    if "." not in text:
        return text
    trimmed = text.rstrip("0").rstrip(".")
    return trimmed or "0"


def fee(value: Any) -> str:
    """Fee with at least six decimals and at most eight (``0.004502``, never ``0.00``)."""
    number = _as_decimal(value)
    if number is None:
        return "" if value is None else str(value)
    text = format(number.quantize(_step(_MAX_FEE_DECIMALS), rounding=ROUND_HALF_UP), "f")
    whole, _, fraction = text.partition(".")
    fraction = fraction.rstrip("0").ljust(_MIN_FEE_DECIMALS, "0")
    return f"{whole}.{fraction}"


def _clean(value: Any, key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {name: _clean(item, name) for name, item in value.items()}
    if isinstance(value, str):
        stripped = value.strip()
        if _NUMERIC.match(stripped):
            number = Decimal(stripped)
            if key in _FEE_KEYS:
                return fee(number)
            # Everything else keeps two decimals when it reads as money, and the full
            # quantity precision below one unit, where two decimals would round to zero.
            return money(number) if abs(number) >= 1 else qty(number)
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_clean(item, key) for item in value]
    return value


def clean_payload(value: Any) -> Any:
    """Recursively round numeric strings in a JSON payload for display."""
    return _clean(value)
