"""Number formatting for the dashboard.

The ledger and the audit trail hold exact ``Decimal`` values, so a raw amount can
arrive at the template as ``9999.09955000000000010018009``. Rounding is a
presentation concern and happens here, at the very edge:

* ``money`` — fiat/USD amounts (equity, PnL, notional, budgets): always 2 decimals.
* ``price`` — instrument prices (entry, stop, target): up to 4 decimals.
* ``qty`` — crypto quantities: up to 6 decimals, trailing zeros removed.
* ``clean_payload`` — the same rounding applied recursively to a JSON payload.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

__all__ = ["clean_payload", "money", "price", "qty"]

_MONEY_STEP = Decimal("0.01")
_PRICE_STEP = Decimal("0.0001")
_QTY_STEP = Decimal("0.000001")
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _fixed(value: Any, step: Decimal) -> str:
    number = _as_decimal(value)
    if number is None:
        return "" if value is None else str(value)
    return str(number.quantize(step, rounding=ROUND_HALF_UP))


def money(value: Any) -> str:
    """Fiat-style amount with exactly two decimals (``9999.0995...`` -> ``9999.10``)."""
    return _fixed(value, _MONEY_STEP)


def price(value: Any) -> str:
    """Instrument price with up to four decimals (``30015.0000000001`` -> ``30015.0000``)."""
    return _fixed(value, _PRICE_STEP)


def qty(value: Any) -> str:
    """Crypto quantity with up to six decimals, trailing zeros removed (``0.0600`` -> ``0.06``)."""
    text = _fixed(value, _QTY_STEP)
    if "." not in text:
        return text
    trimmed = text.rstrip("0").rstrip(".")
    return trimmed or "0"


def _clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, str):
        stripped = value.strip()
        if _NUMERIC.match(stripped):
            number = Decimal(stripped)
            # Amounts of one unit or more read as fiat; fractions read as quantities.
            return money(number) if abs(number) >= 1 else qty(number)
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_clean(item) for item in value]
    return value


def clean_payload(value: Any) -> Any:
    """Recursively round numeric strings in a JSON payload for display."""
    return _clean(value)
