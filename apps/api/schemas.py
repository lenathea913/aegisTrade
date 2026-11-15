"""Response models for the REST surface.

Money and quantities are serialised as **strings**, not floats: a dashboard that
receives ``"0.02"`` cannot silently lose precision, and clients that want numbers
parse the string with a decimal type. Nothing here recomputes money — the ledger
already did.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "HealthResponse",
    "JournalEntryResponse",
    "JournalResponse",
    "LimitsResponse",
    "PositionResponse",
    "PositionsResponse",
    "RiskBudgetResponse",
]


class HealthResponse(BaseModel):
    """Liveness plus the facts an operator checks first."""

    status: Literal["ok", "degraded"]
    version: str
    environment: str
    execution_mode: str
    database: Literal["ok", "unavailable"]
    kill_switch: bool
    time: datetime


class PositionResponse(BaseModel):
    """One current position, marked by the ledger."""

    symbol: str
    side: Literal["BUY", "SELL"] | None
    qty: str
    avg_entry: str
    notional: str
    unrealized_pnl: str
    realized_pnl: str


class PositionsResponse(BaseModel):
    count: int
    positions: list[PositionResponse]


class LimitsResponse(BaseModel):
    """Configured risk limits, echoed in percent units."""

    max_risk_per_trade_pct: str
    max_daily_loss_pct: str
    max_weekly_loss_pct: str
    max_exposure_per_symbol_pct: str
    max_total_exposure_pct: str
    max_leverage: str
    allowed_symbols: list[str]


class RiskBudgetResponse(BaseModel):
    """How much room the configured limits leave right now."""

    equity: str
    risk_per_trade_capacity: str
    daily_realized_pnl: str
    daily_loss_limit: str
    daily_loss_remaining: str
    weekly_realized_pnl: str
    weekly_loss_limit: str
    weekly_loss_remaining: str
    total_exposure: str
    total_exposure_pct: str
    max_total_exposure: str
    leverage: str
    open_positions: int
    kill_switch: bool
    limits: LimitsResponse


class JournalEntryResponse(BaseModel):
    """One row of the merged trade/audit journal."""

    kind: Literal["trade", "audit"]
    at: datetime
    title: str
    detail: dict[str, Any] = Field(default_factory=dict)


class JournalResponse(BaseModel):
    count: int
    limit: int
    entries: list[JournalEntryResponse]
