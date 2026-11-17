"""The execution gateway: the only door from an approval to a broker.

Guarantees enforced here, in order:

1. **Only an ``ApprovedOrder`` gets through.** The type says so, and a runtime
   ``isinstance`` check backs it up for untyped callers (``Any``, deserialised
   payloads, tests): everything else raises ``UnapprovedOrderError``.
2. **Live routing needs a second, independent switch.** Even with a live broker
   configured, nothing is sent unless ``allow_live_orders`` is true.
3. **A price is required.** No current quote and no reference price means the
   order cannot be evaluated, so it is refused rather than guessed.
4. **The order is built by ``Order.from_approved``**, so the approval id travels
   with it into the database and the audit trail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from aegis_core import (
    ApprovedOrder,
    MarketContext,
    Order,
    PositionSide,
    Side,
    Trade,
    UnapprovedOrderError,
    utc_now,
)
from aegis_execution.broker import Broker, BrokerFill
from aegis_execution.errors import ExecutionError, LiveTradingDisabledError

__all__ = ["ExecutionGateway", "ExecutionResult", "ProtectionResult"]


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of routing one approved order."""

    approved: ApprovedOrder
    order: Order
    fill: BrokerFill | None
    trade: Trade | None

    @property
    def filled(self) -> bool:
        return self.fill is not None and self.trade is not None


@dataclass(frozen=True)
class ProtectionResult:
    """Venue order ids of the exits attached to a leg."""

    stop_order_id: str | None = None
    take_order_id: str | None = None

    @property
    def any_placed(self) -> bool:
        return self.stop_order_id is not None or self.take_order_id is not None


class ExecutionGateway:
    """Routes approved orders to a broker and records what came back."""

    def __init__(
        self,
        broker: Broker,
        *,
        allow_live_orders: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._broker = broker
        self._allow_live_orders = allow_live_orders
        self._clock = clock

    @property
    def broker_name(self) -> str:
        return self._broker.name

    @property
    def places_protection(self) -> bool:
        """True when the venue can hold the stop itself (futures, not paper)."""
        return callable(getattr(self._broker, "submit_protection_order", None))

    def protect(
        self,
        *,
        symbol: str,
        side: Side,
        position_side: PositionSide,
        qty: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal | None,
    ) -> ProtectionResult | None:
        """Ask the venue to hold the exits for an open leg.

        ``None`` means this broker has no venue-side protection (paper, spot); the
        caller still has the in-process monitor, so that is not an error.
        """
        place = getattr(self._broker, "submit_protection_order", None)
        if not callable(place):
            return None
        if getattr(self._broker, "live", False) and not self._allow_live_orders:
            raise LiveTradingDisabledError(
                "live orders are disabled (execution.allow_live_orders = false)"
            )
        placed = place(
            symbol=symbol,
            side=side,
            position_side=position_side,
            qty=qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        return ProtectionResult(
            stop_order_id=getattr(placed, "stop_order_id", None),
            take_order_id=getattr(placed, "take_order_id", None),
        )

    def cancel_protection(self, symbol: str) -> int:
        """Remove venue-side exits for ``symbol``; ``1``..n cancelled, ``0`` nothing."""
        cancel = getattr(self._broker, "cancel_protection_orders", None)
        if not callable(cancel):
            return 0
        cancelled = cancel(symbol)
        return int(cancelled or 0)

    def execute(self, approved: ApprovedOrder, *, market: MarketContext) -> ExecutionResult:
        """Send one approved order; raise rather than trade on unclear input."""
        if not isinstance(approved, ApprovedOrder):
            raise UnapprovedOrderError(
                "ExecutionGateway only accepts ApprovedOrder; "
                "an OrderIntent must pass RiskEngine first"
            )
        if getattr(self._broker, "live", False) and not self._allow_live_orders:
            raise LiveTradingDisabledError(
                "live orders are disabled (execution.allow_live_orders = false)"
            )

        price = market.price(approved.symbol) or approved.intent.price
        if price is None:
            raise ExecutionError(
                f"no reference price for {approved.symbol}: cannot execute without a quote"
            )

        order = Order.from_approved(approved, broker=self._broker.name)
        fill = self._broker.submit(order, market_price=price, now=self._clock())
        if fill is None:
            # Accepted but not filled (a limit the market never reached).
            return ExecutionResult(approved=approved, order=order, fill=None, trade=None)

        order = order.mark_submitted(fill.exchange_order_id).apply_fill(fill.qty, fill.price)
        trade = Trade(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            qty=fill.qty,
            price=fill.price,
            fee=fill.fee,
            filled_at=fill.filled_at,
            strategy_id=approved.strategy_id,
            # The leverage the gate approved is the leverage the trade is booked at.
            leverage=approved.intent.leverage,
        )
        return ExecutionResult(approved=approved, order=order, fill=fill, trade=trade)
