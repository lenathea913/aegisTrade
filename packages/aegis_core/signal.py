"""Signals: a proposal that has not been ordered and cannot be ordered directly.

A ``Signal`` is what a dashboard shows the operator — direction, levels, the R:R those
levels imply, why the strategy fired, and a hard expiry. It is **not** an
``OrderIntent``: converting one is a separate, gated action that runs the risk engine
again and produces a *draft* intent. No object here can reach a broker.

``status`` records what the platform decided about the idea:

``NEW``             generated, risk preview not run yet
``RISK_OK``         the risk engine would approve an order built from it
``BLOCKED_BY_RISK`` the risk engine would refuse; ``status_reasons`` says why
``CONVERTED``       turned into a draft intent after a second, passing risk check
``EXPIRED``         past ``expires_at`` — a stale idea is not tradeable
``IGNORED``         dismissed by the operator
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import Field, model_validator

from aegis_core.base import StrictModel
from aegis_core.clock import utc_now
from aegis_core.enums import Side, Timeframe
from aegis_core.ids import new_id
from aegis_core.types import (
    NonEmptyStr,
    PositiveDecimal,
    StrategyId,
    SymbolStr,
    UtcDatetime,
)

__all__ = ["Signal", "SignalDirection", "SignalStatus"]


class SignalStatus(StrEnum):
    """Lifecycle of a signal."""

    NEW = "NEW"
    RISK_OK = "RISK_OK"
    BLOCKED_BY_RISK = "BLOCKED_BY_RISK"
    CONVERTED = "CONVERTED"
    EXPIRED = "EXPIRED"
    IGNORED = "IGNORED"


class SignalDirection(StrEnum):
    """Which way the idea points (spot-friendly naming, not order sides)."""

    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def side(self) -> Side:
        return Side.BUY if self is SignalDirection.LONG else Side.SELL


class Signal(StrictModel):
    """An immutable trading idea with levels, rationale and an expiry."""

    signal_id: NonEmptyStr = Field(default_factory=lambda: new_id("sig"))
    symbol: SymbolStr
    timeframe: Timeframe
    strategy_id: StrategyId
    strategy_version: NonEmptyStr
    direction: SignalDirection
    entry_ref: PositiveDecimal
    stop_loss: PositiveDecimal
    take_profit: PositiveDecimal
    risk_reward: PositiveDecimal
    reason_codes: tuple[NonEmptyStr, ...] = ()
    market_context: dict[str, Any] = Field(default_factory=dict)
    created_at: UtcDatetime = Field(default_factory=utc_now)
    expires_at: UtcDatetime
    status: SignalStatus = SignalStatus.NEW
    status_reasons: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _levels_and_lifetime(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("signals must expire after they are created")

        if self.direction is SignalDirection.LONG:
            if not self.stop_loss < self.entry_ref < self.take_profit:
                raise ValueError("LONG signal needs stop_loss < entry_ref < take_profit")
        else:
            if not self.stop_loss > self.entry_ref > self.take_profit:
                raise ValueError("SHORT signal needs stop_loss > entry_ref > take_profit")
        return self

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    @property
    def is_actionable(self) -> bool:
        """True when a conversion attempt is allowed to reach the risk engine."""
        return self.status in (SignalStatus.NEW, SignalStatus.RISK_OK)

    def with_status(self, status: SignalStatus, reasons: tuple[str, ...] = ()) -> Signal:
        """Return a copy in a new status; signals are values, never mutated in place."""
        return self.model_copy(update={"status": status, "status_reasons": tuple(reasons)})

    @staticmethod
    def risk_reward_for(entry: Decimal, stop: Decimal, target: Decimal) -> Decimal:
        """Reward-to-risk implied by the three levels (always positive by construction)."""
        risk = abs(entry - stop)
        reward = abs(target - entry)
        return reward / risk
