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

from aegis_core import ApprovedOrder, MarketContext, Order, Trade, UnapprovedOrderError, utc_now
from aegis_execution.broker import Broker, BrokerFill
from aegis_execution.errors import ExecutionError, LiveTradingDisabledError

__all__ = ["ExecutionGateway", "ExecutionResult"]


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
        )
        return ExecutionResult(approved=approved, order=order, fill=fill, trade=trade)
