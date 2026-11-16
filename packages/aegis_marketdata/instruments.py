"""Instrument precision, taken from the venue instead of guessed.

A formatter cannot know how many decimals an instrument uses: BTC/USDT quotes with two
(plus a tick of 0.01) while SHIB/USDT needs six or more, and the quantity step differs
per symbol. Hardcoding four decimals for every price renders a cheap asset as
``0.0000`` — a wrong number, not merely a long one.

This module holds the pure part: the ``InstrumentSpec`` value, parsing of Binance's
``exchangeInfo`` payload and a thread-safe cache. Fetching and priming live in the
process that needs it (see ``apps/api/instruments.py``), so the domain stays I/O-free.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from aegis_core.types import normalise_symbol

__all__ = [
    "DEFAULT_PRICE_DECIMALS",
    "DEFAULT_QTY_DECIMALS",
    "InstrumentRegistry",
    "InstrumentSpec",
    "parse_exchange_info",
]

#: Fallback when a symbol is not in the venue metadata yet. Two decimals matches the
#: common USDT pair; the registry replaces it as soon as ``exchangeInfo`` is loaded.
DEFAULT_PRICE_DECIMALS = 2
DEFAULT_QTY_DECIMALS = 6


def _decimals(step: Decimal) -> int:
    """Number of decimal places a step size implies (``0.01000000`` -> 2)."""
    exponent = step.normalize().as_tuple().exponent
    return max(0, -int(exponent))


@dataclass(frozen=True)
class InstrumentSpec:
    """The precision the venue accepts for one symbol."""

    symbol: str
    tick_size: Decimal
    step_size: Decimal

    @property
    def price_decimals(self) -> int:
        return _decimals(self.tick_size)

    @property
    def qty_decimals(self) -> int:
        return _decimals(self.step_size)


class InstrumentRegistry:
    """Thread-safe symbol -> precision cache with a documented fallback."""

    def __init__(self, specs: Mapping[str, InstrumentSpec] | None = None) -> None:
        self._specs: dict[str, InstrumentSpec] = dict(specs or {})

    def __len__(self) -> int:
        return len(self._specs)

    def get(self, symbol: str | None) -> InstrumentSpec | None:
        if not symbol:
            return None
        return self._specs.get(normalise_symbol(symbol))

    def price_decimals(self, symbol: str | None) -> int:
        spec = self.get(symbol)
        return DEFAULT_PRICE_DECIMALS if spec is None else spec.price_decimals

    def qty_decimals(self, symbol: str | None) -> int:
        spec = self.get(symbol)
        return DEFAULT_QTY_DECIMALS if spec is None else spec.qty_decimals

    def replace(self, specs: Mapping[str, InstrumentSpec]) -> None:
        """Swap the whole cache (called once after a successful fetch)."""
        self._specs = dict(specs)


def _filter_value(entry: Mapping[str, Any], filter_type: str, key: str) -> Decimal | None:
    for item in entry.get("filters") or ():
        if not isinstance(item, Mapping) or item.get("filterType") != filter_type:
            continue
        raw = item.get(key)
        if raw is None:
            continue
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
    return None


def parse_exchange_info(payload: Mapping[str, Any]) -> dict[str, InstrumentSpec]:
    """Turn an ``exchangeInfo`` payload into ``BASE/QUOTE -> InstrumentSpec``.

    Only symbols that are trading and carry both a price tick and a lot step are kept:
    a provisional or delisted symbol must not set the precision of a live one.
    """
    specs: dict[str, InstrumentSpec] = {}
    for entry in payload.get("symbols") or ():
        if not isinstance(entry, Mapping):
            continue
        status = entry.get("status")
        if status is not None and status != "TRADING":
            continue
        base, quote = entry.get("baseAsset"), entry.get("quoteAsset")
        if not base or not quote:
            continue
        tick = _filter_value(entry, "PRICE_FILTER", "tickSize")
        step = _filter_value(entry, "LOT_SIZE", "stepSize")
        if tick is None or step is None or tick <= 0 or step <= 0:
            continue
        try:
            symbol = normalise_symbol(f"{base}/{quote}")
        except ValueError:
            continue
        specs[symbol] = InstrumentSpec(symbol=symbol, tick_size=tick, step_size=step)
    return specs
