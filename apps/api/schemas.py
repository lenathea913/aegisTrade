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

from aegis_core import OrderIntent, Signal

__all__ = [
    "CancelOrderResponse",
    "ConversionResponse",
    "ExchangeOrderListResponse",
    "ExchangeOrderResponse",
    "ExchangeStatusResponse",
    "HealthResponse",
    "IntentResponse",
    "JournalEntryResponse",
    "JournalResponse",
    "LimitsResponse",
    "MotivationResponse",
    "PositionResponse",
    "PositionsResponse",
    "RiskBudgetResponse",
    "SignalListResponse",
    "SignalResponse",
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


class MotivationResponse(BaseModel):
    """One process reminder, for the dashboard footer and the API."""

    quote: str


class SignalResponse(BaseModel):
    """A trading idea and what the risk engine thinks of it so far."""

    signal_id: str
    symbol: str
    timeframe: str
    strategy_id: str
    strategy_version: str
    direction: str
    entry_ref: str
    stop_loss: str
    take_profit: str
    risk_reward: str
    reason_codes: list[str]
    market_context: dict[str, Any]
    status: str
    status_reasons: list[str]
    created_at: datetime
    expires_at: datetime

    @classmethod
    def from_domain(cls, signal: Signal) -> SignalResponse:
        """Map the domain object, keeping money as strings."""
        return cls(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            timeframe=signal.timeframe.value,
            strategy_id=signal.strategy_id,
            strategy_version=signal.strategy_version,
            direction=signal.direction.value,
            entry_ref=str(signal.entry_ref),
            stop_loss=str(signal.stop_loss),
            take_profit=str(signal.take_profit),
            risk_reward=str(signal.risk_reward),
            reason_codes=list(signal.reason_codes),
            market_context=dict(signal.market_context),
            status=signal.status.value,
            status_reasons=list(signal.status_reasons),
            created_at=signal.created_at,
            expires_at=signal.expires_at,
        )


class SignalListResponse(BaseModel):
    count: int
    signals: list[SignalResponse]


class IntentResponse(BaseModel):
    """A **draft** order intent: nothing has been ordered."""

    symbol: str
    side: str
    order_type: str
    qty: str
    price: str | None
    stop_loss: str
    take_profit: str | None
    strategy_id: str
    reason: str
    created_at: datetime

    @classmethod
    def from_domain(cls, intent: OrderIntent) -> IntentResponse:
        return cls(
            symbol=intent.symbol,
            side=intent.side.value,
            order_type=intent.order_type.value,
            qty=str(intent.qty),
            price=None if intent.price is None else str(intent.price),
            stop_loss=str(intent.stop_loss),
            take_profit=None if intent.take_profit is None else str(intent.take_profit),
            strategy_id=intent.strategy_id,
            reason=intent.reason,
            created_at=intent.created_at,
        )


class ExchangeStatusResponse(BaseModel):
    """Where orders would go right now, and whether that venue answers."""

    mode: str
    label: str
    broker: str
    connected: bool
    detail: str
    checked_at: datetime


class ExchangeOrderResponse(BaseModel):
    """One order still live on the exchange (not yet final in the ledger)."""

    order_id: str
    symbol: str
    side: str | None
    status: str
    price: str | None
    qty: str | None
    filled_qty: str


class ExchangeOrderListResponse(BaseModel):
    count: int
    orders: list[ExchangeOrderResponse]


class CancelOrderResponse(BaseModel):
    order_id: str
    status: str
    cancelled: Literal[True] = True


class ClosePositionResponse(BaseModel):
    """What the manual close did: an opposite-side intent walked through the gate."""

    symbol: str
    side: str
    qty: str
    price: str | None
    approved: bool
    filled: bool
    order_id: str | None
    reasons: list[str]


class ConversionResponse(BaseModel):
    """Result of converting a signal: a draft intent plus the gate's checks."""

    signal: SignalResponse
    intent: IntentResponse
    status: Literal["DRAFT"] = "DRAFT"
    max_risk_amount: str
    checks: list[str]
