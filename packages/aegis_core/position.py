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
from aegis_core.leverage import MAX_LEVERAGE, liquidation_price, margin_for, roe_pct
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
    #: Margin multiplier of the open leg (1 = spot). Reserved for the leg's lifetime.
    leverage: int = Field(default=1, ge=1, le=MAX_LEVERAGE)
    #: Protective exit levels, carried with the leg so they survive a restart. A flat
    #: position has none: there is no exposure for a stop to protect.
    stop_loss: FiniteDecimal | None = None
    take_profit: FiniteDecimal | None = None
    opened_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _flat_has_no_cost_basis(self) -> Self:
        if self.qty == 0 and self.avg_entry != 0:
            raise ValueError("a flat position (qty == 0) must have avg_entry == 0")
        if self.qty != 0 and self.avg_entry == 0:
            raise ValueError("a non-flat position must have a positive avg_entry")
        if self.qty == 0 and self.leverage != 1:
            raise ValueError("a flat position posts no margin, so leverage must be 1")
        if self.qty == 0 and (self.stop_loss is not None or self.take_profit is not None):
            raise ValueError("a flat position cannot carry protective exit levels")
        if self.stop_loss is not None and self.stop_loss <= 0:
            raise ValueError("stop_loss must be greater than 0")
        if self.take_profit is not None and self.take_profit <= 0:
            raise ValueError("take_profit must be greater than 0")
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

    @property
    def margin_used(self) -> Decimal:
        """Isolated collateral committed by this position (``notional / leverage``)."""
        return margin_for(self.notional, self.leverage)

    @property
    def liquidation_price(self) -> Decimal | None:
        """Estimated liquidation price, or ``None`` for a flat book."""
        if self.is_flat or self.side is None:
            return None
        return liquidation_price(self.avg_entry, self.side, self.leverage)

    @property
    def roe_pct(self) -> Decimal:
        """Unrealized PnL as a percentage of the posted margin."""
        return roe_pct(self.unrealized_pnl, self.margin_used)

    def with_mark(self, price: Decimal) -> Position:
        """Return the position re-marked at ``price`` (unrealized PnL only)."""
        if price <= 0:
            raise DomainError("mark price must be greater than 0")
        if self.is_flat:
            return self.model_copy(update={"unrealized_pnl": _ZERO})
        return self.model_copy(update={"unrealized_pnl": (price - self.avg_entry) * self.qty})

    def apply_fill(self, qty: Decimal, price: Decimal, *, leverage: int | None = None) -> Position:
        """Apply a signed fill (``+`` buy, ``-`` sell) at ``price``.

        ``leverage`` applies only when the fill *opens* a leg: a reduction keeps the
        leverage the position was opened with, because margin was posted then.

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
                    "leverage": self.leverage if leverage is None else leverage,
                }
            )

        closed_qty = min(self.abs_qty, abs(qty))
        direction = Decimal(1) if self.qty > 0 else Decimal(-1)
        realized = self.realized_pnl + (price - self.avg_entry) * closed_qty * direction

        if new_qty == 0:
            # Fully closed: nothing is left to protect.
            return self.model_copy(
                update={
                    "qty": _ZERO,
                    "avg_entry": _ZERO,
                    "unrealized_pnl": _ZERO,
                    "realized_pnl": realized,
                    "leverage": 1,
                    "stop_loss": None,
                    "take_profit": None,
                }
            )
        if (new_qty > 0) == (self.qty > 0):
            # Partial close: cost basis of the remainder is unchanged.
            return self.model_copy(
                update={"qty": new_qty, "realized_pnl": realized, "unrealized_pnl": _ZERO}
            )
        # Flipped through zero: the new leg opens at the fill price, and the old leg's
        # levels belong to the leg that was just closed.
        return self.model_copy(
            update={
                "qty": new_qty,
                "avg_entry": price,
                "unrealized_pnl": _ZERO,
                "realized_pnl": realized,
                "leverage": self.leverage if leverage is None else leverage,
                "stop_loss": None,
                "take_profit": None,
            }
        )

    def close(self, price: Decimal) -> Position:
        """Flatten the position at ``price`` and realize PnL."""
        if self.is_flat:
            raise DomainError("position is already flat")
        return self.apply_fill(-self.qty, price)
