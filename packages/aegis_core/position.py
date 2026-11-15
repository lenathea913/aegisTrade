"""Position domain.

A position is **signed**: positive is long, negative is short, zero is flat. Using
one signed number (instead of separate long/short objects) keeps the ledger maths
single-branch and makes "flip through zero" an explicit, tested case.

Fees are *not* handled here — they belong to ``PortfolioLedger``/``Trade``. A
position only knows cost basis and PnL.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from aegis_core.base import StrictModel
from aegis_core.clock import utc_now
from aegis_core.enums import Side
from aegis_core.errors import DomainError
from aegis_core.types import FiniteDecimal, NonNegativeDecimal, SymbolStr, UtcDatetime

__all__ = ["Position"]

_ZERO = Decimal("0")


class Position(StrictModel):
    """Current exposure in one symbol.

    Invariants:
        * ``avg_entry`` is positive for any non-flat position and always exact
          (weighted average, unrounded — rounding is a presentation concern).
        * ``realized_pnl`` accumulates over the life of the position and only
          changes when quantity is *reduced*.
    """

    symbol: SymbolStr
    qty: FiniteDecimal = _ZERO
    avg_entry: NonNegativeDecimal = _ZERO
    unrealized_pnl: FiniteDecimal = _ZERO
    realized_pnl: FiniteDecimal = _ZERO
    opened_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _flat_has_no_cost_basis(self) -> Self:
        if self.qty == 0 and self.avg_entry != 0:
            raise ValueError("a flat position (qty == 0) must have avg_entry == 0")
        if self.qty != 0 and self.avg_entry == 0:
            raise ValueError("a non-flat position must have a positive avg_entry")
        return self

    @classmethod
    def flat(cls, symbol: str) -> Position:
        """An empty position for ``symbol``."""
        return cls(symbol=symbol)

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def side(self) -> Side | None:
        if self.qty == 0:
            return None
        return Side.BUY if self.qty > 0 else Side.SELL

    @property
    def abs_qty(self) -> Decimal:
        return abs(self.qty)

    @property
    def notional(self) -> Decimal:
        """Exposure at cost basis (absolute, quote currency)."""
        return self.abs_qty * self.avg_entry

    def with_mark(self, price: Decimal) -> Position:
        """Return the position re-marked at ``price`` (unrealized PnL only)."""
        if price <= 0:
            raise DomainError("mark price must be greater than 0")
        if self.is_flat:
            return self.model_copy(update={"unrealized_pnl": _ZERO})
        return self.model_copy(update={"unrealized_pnl": (price - self.avg_entry) * self.qty})

    def apply_fill(self, qty: Decimal, price: Decimal) -> Position:
        """Apply a signed fill (``+`` buy, ``-`` sell) at ``price``.

        Three cases, all tested:
            * same direction (or opening) -> quantity grows, cost basis is the
              volume-weighted average;
            * shrinking -> quantity shrinks, ``realized_pnl`` grows, basis is kept;
            * crossing zero -> the old position is fully realized and a new one is
              opened at ``price`` with the residual quantity.

        ``unrealized_pnl`` is reset to zero because the mark is now stale.
        """
        if qty == 0:
            raise DomainError("fill qty must not be zero")
        if price <= 0:
            raise DomainError("fill price must be greater than 0")

        new_qty = self.qty + qty
        opening = self.is_flat or (self.qty > 0) == (qty > 0)

        if opening:
            new_abs = abs(self.qty) + abs(qty)
            avg_entry = (self.abs_qty * self.avg_entry + abs(qty) * price) / new_abs
            return self.model_copy(
                update={
                    "qty": new_qty,
                    "avg_entry": avg_entry,
                    "unrealized_pnl": _ZERO,
                }
            )

        closed_qty = min(self.abs_qty, abs(qty))
        direction = Decimal(1) if self.qty > 0 else Decimal(-1)
        realized = self.realized_pnl + (price - self.avg_entry) * closed_qty * direction

        if new_qty == 0:
            return self.model_copy(
                update={
                    "qty": _ZERO,
                    "avg_entry": _ZERO,
                    "unrealized_pnl": _ZERO,
                    "realized_pnl": realized,
                }
            )
        if (new_qty > 0) == (self.qty > 0):
            # Partial close: cost basis of the remainder is unchanged.
            return self.model_copy(
                update={"qty": new_qty, "realized_pnl": realized, "unrealized_pnl": _ZERO}
            )
        # Flipped through zero: the new leg opens at the fill price.
        return self.model_copy(
            update={
                "qty": new_qty,
                "avg_entry": price,
                "unrealized_pnl": _ZERO,
                "realized_pnl": realized,
            }
        )

    def close(self, price: Decimal) -> Position:
        """Flatten the position at ``price`` and realize PnL."""
        if self.is_flat:
            raise DomainError("position is already flat")
        return self.apply_fill(-self.qty, price)
