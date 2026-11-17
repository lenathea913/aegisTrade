"""Order domain: proposal -> approval -> submitted order.

The three types encode the platform's most important boundary:

``OrderIntent``      what a strategy *wants* to do (never executable)
      |
      v  RiskEngine approves or rejects
``ApprovedOrder``    intent + approval evidence + the amount at risk
      |
      v  ExecutionGateway is the only consumer
``Order``            the broker-side record and its lifecycle

``Order.from_approved()`` is the only constructor for an executable order, which is
what makes "no order without risk approval" a type-level guarantee rather than a
convention.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from aegis_core.base import StrictModel
from aegis_core.clock import utc_now
from aegis_core.enums import OrderStatus, OrderType, PositionSide, Side
from aegis_core.errors import DomainError, InvalidStateTransitionError
from aegis_core.ids import new_id
from aegis_core.leverage import MAX_LEVERAGE, liquidation_price, margin_for
from aegis_core.types import (
    NonEmptyStr,
    NonNegativeDecimal,
    PositiveDecimal,
    Reason,
    StrategyId,
    SymbolStr,
    UtcDatetime,
)

__all__ = ["ApprovedOrder", "Order", "OrderIntent"]


class OrderIntent(StrictModel):
    """A strategy's proposal to trade.

    Invariants:
        * ``qty`` and ``stop_loss`` are mandatory and positive: every entry carries
          an explicit invalidation level, so risk can always be measured.
        * ``LIMIT`` requires ``price``; ``MARKET`` may carry a reference price.
        * A stop/take-profit that sits on the wrong side of the reference price is
          a bug in the strategy, not a market condition, and is rejected here.
        * ``leverage`` is the margin multiplier the intent wants (1 = spot). It is
          capped by the domain, not by configuration, so no caller can ask a broker
          for more than :data:`aegis_core.leverage.MAX_LEVERAGE`.
        * ``position_side`` is set for futures hedge mode: an order may open a leg or
          close it, and only the intent knows which was meant.
    """

    symbol: SymbolStr
    side: Side
    order_type: OrderType
    qty: PositiveDecimal
    price: PositiveDecimal | None = None
    stop_loss: PositiveDecimal
    take_profit: PositiveDecimal | None = None
    strategy_id: StrategyId
    reason: Reason
    #: Margin multiplier (1 = spot-equivalent), never above ``MAX_LEVERAGE``.
    leverage: int = Field(default=1, ge=1, le=MAX_LEVERAGE)
    #: Futures leg this order belongs to; ``None`` means "derive it from the side".
    position_side: PositionSide | None = None
    created_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_levels(self) -> Self:
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("LIMIT order requires a price")

        reference = self.price
        if reference is None:
            return self

        if self.stop_loss == reference:
            raise ValueError("stop_loss must differ from the reference price")

        if self.side is Side.BUY:
            if self.stop_loss > reference:
                raise ValueError("BUY stop_loss must be below the entry price")
            if self.take_profit is not None and self.take_profit <= reference:
                raise ValueError("BUY take_profit must be above the entry price")
        else:
            if self.stop_loss < reference:
                raise ValueError("SELL stop_loss must be above the entry price")
            if self.take_profit is not None and self.take_profit >= reference:
                raise ValueError("SELL take_profit must be below the entry price")

        return self

    @property
    def risk_per_unit(self) -> Decimal | None:
        """Distance to the stop per unit, or ``None`` when no price is known yet."""
        if self.price is None:
            return None
        return abs(self.price - self.stop_loss)

    def risk_amount(self, entry_price: Decimal | None = None) -> Decimal | None:
        """Amount at risk if the stop is hit, using ``entry_price`` (default: ``price``)."""
        entry = entry_price if entry_price is not None else self.price
        if entry is None:
            return None
        return abs(entry - self.stop_loss) * self.qty

    def notional(self, price: Decimal | None = None) -> Decimal | None:
        """Position notional at ``price`` (default: ``price``)."""
        reference = price if price is not None else self.price
        if reference is None:
            return None
        return self.qty * reference

    @property
    def resolved_position_side(self) -> PositionSide:
        """The futures leg: explicit when given, otherwise implied by the side."""
        return self.position_side or PositionSide.for_entry(self.side)

    @property
    def is_reducing(self) -> bool:
        """True when the order closes an existing leg rather than opening one."""
        return self.position_side is not None and self.position_side.closing_side is self.side

    @property
    def margin(self) -> Decimal | None:
        """Isolated margin this intent would post (``notional / leverage``)."""
        notional = self.notional()
        return None if notional is None else margin_for(notional, self.leverage)

    def liquidation_price(self, entry: Decimal | None = None) -> Decimal | None:
        """Estimated liquidation price at this leverage (``None`` without a price)."""
        reference = entry if entry is not None else self.price
        if reference is None:
            return None
        return liquidation_price(reference, self.side, self.leverage)


class ApprovedOrder(StrictModel):
    """An ``OrderIntent`` that passed the risk gate, with the evidence attached.

    Only ``RiskEngine`` constructs these, and ``ExecutionGateway`` only accepts
    these. ``risk_checks_passed`` records *which* checks ran so a later audit can
    reconstruct the decision even after limits change.
    """

    intent: OrderIntent
    approval_id: NonEmptyStr = Field(default_factory=lambda: new_id("appr"))
    risk_checks_passed: tuple[NonEmptyStr, ...] = ()
    max_risk_amount: PositiveDecimal
    approved_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _approval_follows_intent(self) -> Self:
        if self.approved_at < self.intent.created_at:
            raise ValueError("approved_at must not precede intent.created_at")
        return self

    # Flat read-only views so consumers do not need to reach through ``.intent``.
    @property
    def symbol(self) -> str:
        return self.intent.symbol

    @property
    def side(self) -> Side:
        return self.intent.side

    @property
    def order_type(self) -> OrderType:
        return self.intent.order_type

    @property
    def qty(self) -> Decimal:
        return self.intent.qty

    @property
    def price(self) -> Decimal | None:
        return self.intent.price

    @property
    def stop_loss(self) -> Decimal:
        return self.intent.stop_loss

    @property
    def take_profit(self) -> Decimal | None:
        return self.intent.take_profit

    @property
    def strategy_id(self) -> str:
        return self.intent.strategy_id

    @property
    def leverage(self) -> int:
        return self.intent.leverage

    @property
    def position_side(self) -> PositionSide | None:
        return self.intent.position_side

    @property
    def created_at(self) -> datetime:
        return self.intent.created_at


class Order(StrictModel):
    """A broker-side order created from an ``ApprovedOrder``.

    State transitions are explicit methods returning a new instance; invalid
    transitions raise ``InvalidStateTransitionError`` instead of silently corrupting
    state.
    """

    order_id: NonEmptyStr = Field(default_factory=lambda: new_id("ord"))
    approval_id: NonEmptyStr
    symbol: SymbolStr
    side: Side
    order_type: OrderType
    qty: PositiveDecimal
    price: PositiveDecimal | None = None
    status: OrderStatus = OrderStatus.NEW
    broker: NonEmptyStr
    exchange_order_id: NonEmptyStr | None = None
    filled_qty: NonNegativeDecimal = Decimal("0")
    avg_fill_price: PositiveDecimal | None = None
    #: Futures leg, carried from the approval so the broker can address the right leg.
    position_side: PositionSide | None = None
    #: Margin multiplier the broker must set before sending this order.
    leverage: int = Field(default=1, ge=1, le=MAX_LEVERAGE)
    created_at: UtcDatetime = Field(default_factory=utc_now)
    updated_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_fills(self) -> Self:
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("LIMIT order requires a price")
        if self.filled_qty > self.qty:
            raise ValueError("filled_qty cannot exceed qty")
        if self.filled_qty > 0 and self.avg_fill_price is None:
            raise ValueError("avg_fill_price is required once filled_qty > 0")
        if self.filled_qty == 0 and self.avg_fill_price is not None:
            raise ValueError("avg_fill_price must be None while filled_qty is 0")
        if self.status is OrderStatus.FILLED and self.filled_qty != self.qty:
            raise ValueError("a FILLED order must be fully filled")
        return self

    @classmethod
    def from_approved(cls, approved: ApprovedOrder, *, broker: str) -> Order:
        """Build an executable order. Accepts nothing but an ``ApprovedOrder``."""
        return cls(
            approval_id=approved.approval_id,
            symbol=approved.symbol,
            side=approved.side,
            order_type=approved.order_type,
            qty=approved.qty,
            price=approved.price,
            broker=broker,
            position_side=approved.intent.position_side,
            leverage=approved.intent.leverage,
        )

    @property
    def remaining_qty(self) -> Decimal:
        return self.qty - self.filled_qty

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def filled_notional(self) -> Decimal:
        if self.avg_fill_price is None:
            return Decimal("0")
        return self.filled_qty * self.avg_fill_price

    def mark_submitted(self, exchange_order_id: str) -> Self:
        """Record the broker acknowledgement."""
        if self.status is not OrderStatus.NEW:
            raise InvalidStateTransitionError(f"cannot submit an order in status {self.status}")
        if not exchange_order_id.strip():
            raise DomainError("exchange_order_id must not be empty")
        return self.model_copy(
            update={
                "status": OrderStatus.SUBMITTED,
                "exchange_order_id": exchange_order_id,
                "updated_at": utc_now(),
            }
        )

    def apply_fill(self, qty: Decimal, price: Decimal) -> Self:
        """Apply a fill of ``qty`` at ``price``, recomputing the weighted average fill."""
        if self.is_terminal:
            raise InvalidStateTransitionError(f"cannot fill an order in status {self.status}")
        if qty <= 0:
            raise DomainError("fill qty must be greater than 0")
        if price <= 0:
            raise DomainError("fill price must be greater than 0")
        if qty > self.remaining_qty:
            raise DomainError(
                f"fill qty {qty} exceeds remaining qty {self.remaining_qty}",
            )

        new_filled = self.filled_qty + qty
        weighted = self.filled_notional + qty * price
        status = OrderStatus.FILLED if new_filled == self.qty else OrderStatus.PARTIALLY_FILLED
        return self.model_copy(
            update={
                "filled_qty": new_filled,
                "avg_fill_price": weighted / new_filled,
                "status": status,
                "updated_at": utc_now(),
            }
        )

    def cancel(self) -> Self:
        """Cancel the remainder of an order (allowed while not terminal)."""
        if self.is_terminal:
            raise InvalidStateTransitionError(f"cannot cancel an order in status {self.status}")
        return self.model_copy(update={"status": OrderStatus.CANCELED, "updated_at": utc_now()})

    def expire(self) -> Self:
        """Mark an unfilled order as expired."""
        if self.is_terminal:
            raise InvalidStateTransitionError(f"cannot expire an order in status {self.status}")
        return self.model_copy(update={"status": OrderStatus.EXPIRED, "updated_at": utc_now()})

    def reject(self) -> Self:
        """Mark an order as rejected by the broker before any fill."""
        if self.status is not OrderStatus.NEW:
            raise InvalidStateTransitionError(f"cannot reject an order in status {self.status}")
        return self.model_copy(update={"status": OrderStatus.REJECTED, "updated_at": utc_now()})
