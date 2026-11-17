"""Trade domain: one executed fill, as recorded in the journal and ledger."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from aegis_core.base import StrictModel
from aegis_core.clock import utc_now
from aegis_core.enums import Side
from aegis_core.ids import new_id
from aegis_core.leverage import MAX_LEVERAGE
from aegis_core.types import (
    FiniteDecimal,
    NonEmptyStr,
    NonNegativeDecimal,
    PositiveDecimal,
    StrategyId,
    SymbolStr,
    UtcDatetime,
)

__all__ = ["Trade"]

_ZERO = Decimal("0")


class Trade(StrictModel):
    """A single executed trade.

    Sign conventions:
        * ``fee`` is always non-negative (a cost paid).
        * ``funding`` is signed: negative = paid, positive = received.
        * ``r_multiple`` is the realized result expressed in units of the risk that
          was taken at entry (``+2R`` means twice the planned risk). It is optional
          because it can only be computed once a stop was defined.
    """

    trade_id: NonEmptyStr = Field(default_factory=lambda: new_id("trd"))
    order_id: NonEmptyStr
    symbol: SymbolStr
    side: Side
    qty: PositiveDecimal
    price: PositiveDecimal
    fee: NonNegativeDecimal = _ZERO
    funding: FiniteDecimal = _ZERO
    filled_at: UtcDatetime = Field(default_factory=utc_now)
    strategy_id: StrategyId
    r_multiple: FiniteDecimal | None = None
    #: Margin multiplier of the leg this fill belonged to (1 = spot/unleveraged).
    leverage: int = Field(default=1, ge=1, le=MAX_LEVERAGE)

    @property
    def notional(self) -> Decimal:
        return self.qty * self.price

    @property
    def fees_and_funding(self) -> Decimal:
        """Net cost of holding: fees plus funding (negative funding = a credit)."""
        return self.fee + self.funding

    @property
    def signed_qty(self) -> Decimal:
        return self.qty * self.side.sign
