"""Risk domain types: the limits an operator sets, and the gate's answer.

Percentages use percent units (``1.0`` == 1 %). ``RiskLimit`` refuses internally
inconsistent configurations at construction time (e.g. a per-symbol exposure cap
above the total cap), so a broken risk configuration cannot reach the gate.

``allowed_symbols`` defaults to *empty*, which denies every symbol. The safe
default is "trade nothing" — an allow-list must be written down on purpose.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from aegis_core.base import StrictModel
from aegis_core.leverage import MAX_LEVERAGE
from aegis_core.types import (
    ExposurePercent,
    LeverageDecimal,
    NonEmptyStr,
    NonNegativeDecimal,
    Percent,
    SymbolStr,
)

__all__ = ["RiskDecision", "RiskLimit"]

_ZERO = Decimal("0")


class RiskLimit(StrictModel):
    """Hard limits applied to every ``OrderIntent``."""

    max_risk_per_trade_pct: Percent = Decimal("0.5")
    max_daily_loss_pct: Percent = Decimal("2")
    max_weekly_loss_pct: Percent = Decimal("5")
    max_exposure_per_symbol_pct: ExposurePercent = Decimal("20")
    max_total_exposure_pct: ExposurePercent = Decimal("60")
    #: How many positions the book may hold at once. With 20 % per symbol and 60 %
    #: total this is what actually stops a ten-symbol universe from turning every
    #: scan into ten concurrent bets: the caps alone would allow three, and a fourth
    #: idea would be sized to zero rather than judged.
    max_open_positions: int = Field(default=3, ge=1, le=50)
    #: Ceiling for ``OrderIntent.leverage``; the domain cap always wins.
    max_leverage: LeverageDecimal = Decimal("1")
    allowed_symbols: frozenset[SymbolStr] = frozenset()

    @model_validator(mode="after")
    def _validate_consistency(self) -> Self:
        if self.max_weekly_loss_pct < self.max_daily_loss_pct:
            raise ValueError("max_weekly_loss_pct must be >= max_daily_loss_pct")
        if self.max_exposure_per_symbol_pct > self.max_total_exposure_pct:
            raise ValueError(
                "max_exposure_per_symbol_pct must be <= max_total_exposure_pct",
            )
        if self.max_leverage > MAX_LEVERAGE:
            # The cap exists so no configuration mistake can liquidate the account.
            raise ValueError(f"max_leverage must be <= {MAX_LEVERAGE}")
        return self

    def allows(self, symbol: str) -> bool:
        """True when ``symbol`` is on the allow-list (normalised comparison)."""
        from aegis_core.types import normalise_symbol

        return normalise_symbol(symbol) in self.allowed_symbols


class RiskDecision(StrictModel):
    """The risk gate's verdict.

    A rejection must carry at least one reason — an unexplained block is not
    auditable. ``max_risk_amount`` is the amount the engine was willing to risk
    (zero when rejected).
    """

    approved: bool
    reasons: tuple[NonEmptyStr, ...] = ()
    max_risk_amount: NonNegativeDecimal = _ZERO

    @model_validator(mode="after")
    def _rejection_has_reason(self) -> Self:
        if not self.approved and not self.reasons:
            raise ValueError("a rejected RiskDecision must include at least one reason")
        return self

    @classmethod
    def approve(cls, max_risk_amount: Decimal, checks_passed: tuple[str, ...] = ()) -> RiskDecision:
        """Build an approval, optionally recording which checks were evaluated."""
        return cls(approved=True, reasons=checks_passed, max_risk_amount=max_risk_amount)

    @classmethod
    def reject(cls, *reasons: str) -> RiskDecision:
        """Build a rejection. At least one reason is required."""
        return cls(approved=False, reasons=reasons)

    @property
    def is_approved(self) -> bool:
        return self.approved
