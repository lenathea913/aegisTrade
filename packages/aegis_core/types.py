"""Validated scalar types used across the domain.

Design notes
------------
* **Money and quantities are ``Decimal``**, never ``float``. A ledger that rounds
  with binary floats cannot reconcile against an exchange.
* **Percentages are expressed in percent** (``1.5`` == 1.5 %), validated to the
  range ``(0, 100]``. Every consumer must convert explicitly with ``/ 100``; this
  is deliberate so a limit can never be read as a fraction by accident.
* **Naive datetimes are rejected**, aware non-UTC datetimes are converted to UTC.
* Symbols are normalised to ``BASE/QUOTE`` uppercase (``btc-usdt`` -> ``BTC/USDT``).
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, StringConstraints

from aegis_core.clock import to_utc

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,15}(?:/[A-Z0-9]{2,10})?$")


def _finite(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("value must be a finite decimal (no NaN/Infinity)")
    return value


def _positive(value: Decimal) -> Decimal:
    _finite(value)
    if value <= 0:
        raise ValueError("value must be greater than 0")
    return value


def _non_negative(value: Decimal) -> Decimal:
    _finite(value)
    if value < 0:
        raise ValueError("value must be greater than or equal to 0")
    return value


def _signed(value: Decimal) -> Decimal:
    return _finite(value)


def _percent(value: Decimal) -> Decimal:
    _finite(value)
    if value <= 0 or value > 100:
        raise ValueError("percentage must be within (0, 100]")
    return value


_MAX_EXPOSURE_PERCENT = Decimal("1000")


def _exposure_percent(value: Decimal) -> Decimal:
    """Exposure may exceed 100 % (futures), but never absurdly."""
    _finite(value)
    if value <= 0 or value > _MAX_EXPOSURE_PERCENT:
        raise ValueError("exposure percentage must be within (0, 1000]")
    return value


def _leverage(value: Decimal) -> Decimal:
    _finite(value)
    if value < 1:
        raise ValueError("leverage must be at least 1 (spot = 1)")
    return value


def normalise_symbol(value: str) -> str:
    """Normalise a symbol to ``BASE/QUOTE`` uppercase.

    ``"btc-usdt"``, ``" btc/usdt "`` and ``"BTC/USDT"`` all become ``"BTC/USDT"``.
    A bare ticker such as ``"BTCUSDT"`` is kept as-is (no separator is invented).
    """
    normalised = value.strip().upper().replace("-", "/")
    if not _SYMBOL_PATTERN.match(normalised):
        raise ValueError(
            f"invalid symbol {value!r}; expected BASE/QUOTE (e.g. BTC/USDT) or a ticker"
        )
    return normalised


def _to_utc(value: datetime) -> datetime:
    return to_utc(value)


#: Timezone-aware datetime, always converted to UTC.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]

#: Finite decimal, any sign (PnL, funding, deltas).
FiniteDecimal = Annotated[Decimal, AfterValidator(_signed)]

#: Decimal > 0 (quantities, prices, risk amounts).
PositiveDecimal = Annotated[Decimal, AfterValidator(_positive)]

#: Decimal >= 0 (fees, cumulative fills).
NonNegativeDecimal = Annotated[Decimal, AfterValidator(_non_negative)]

#: Percentage in percent, ``(0, 100]``. ``1.0`` means 1 %.
Percent = Annotated[Decimal, AfterValidator(_percent)]

#: Exposure percentage in percent, ``(0, 1000]``. Gross exposure may exceed equity
#: on margin/futures, where 300 % means 3x notional against equity.
ExposurePercent = Annotated[Decimal, AfterValidator(_exposure_percent)]

#: Effective leverage >= 1.
LeverageDecimal = Annotated[Decimal, AfterValidator(_leverage)]

#: Normalised trading symbol.
SymbolStr = Annotated[str, AfterValidator(normalise_symbol)]

#: Non-empty identifier / label after whitespace stripping.
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]

#: Strategy identifier, e.g. ``simple_ema_cross``.
StrategyId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$"
    ),
]

#: Free-text rationale kept for the audit trail.
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
