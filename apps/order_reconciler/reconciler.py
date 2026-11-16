"""Order reconciliation: what the exchange did versus what the book believes.

A real exchange is asynchronous. An order can be accepted and fill later, fill in
pieces, or be cancelled/rejected long after ``submit`` returned. ``TradingPipeline``
can only record what it saw; this job closes the gap:

    open order (NEW / SUBMITTED / PARTIALLY_FILLED)
        -> exchange.fetch()
        -> FILLED          : write the *delta* as a Trade, update the ledger + position
        -> CANCELED/REJECTED: close the order, record why, release any reservation
        -> still open      : say nothing, leave it alone

Only the delta is applied (``state.filled_qty - order.filled_qty``), so an order that
partly filled inside ``TradingPipeline`` is never double-counted. The reconciler never
creates an ``OrderIntent`` or an ``ApprovedOrder``: it only records outcomes of orders
that already passed the gate.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from aegis_core import OrderStatus, Side, Trade, utc_now
from aegis_execution import ExchangeError, ExchangeOrderState
from aegis_ledger import AuditEvent, EventType, LedgerUpdate, LedgerWriter, PortfolioLedger
from aegis_observability import get_logger

__all__ = [
    "ExchangeOrderFeed",
    "OpenOrder",
    "OrderBook",
    "OrderReconciler",
    "ReconciliationResult",
]


@dataclass(frozen=True)
class OpenOrder:
    """One unresolved order as the database knows it."""

    order_id: str
    exchange_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    filled_qty: Decimal
    status: OrderStatus
    strategy_id: str


class OrderBook(Protocol):
    """Persistence the reconciler needs; a fake implements this in tests."""

    def open_orders(self) -> Sequence[OpenOrder]:
        """Orders the exchange may still change."""

    def mark_filled(self, order: OpenOrder, *, filled_qty: Decimal, avg_price: Decimal) -> None:
        """Record the new cumulative fill state on the order row."""

    def mark_closed(self, order: OpenOrder, status: OrderStatus) -> None:
        """Record a terminal status the exchange chose (cancel/reject/expire)."""


class ExchangeOrderFeed(Protocol):
    """The one exchange call the reconciler makes."""

    def fetch(self, exchange_order_id: str, *, symbol: str) -> ExchangeOrderState:
        """Current exchange state of one order."""


@dataclass(frozen=True)
class ReconciliationResult:
    """What changed for one order (or why nothing did)."""

    order_id: str
    symbol: str
    previous: OrderStatus
    current: OrderStatus
    filled_delta: Decimal
    trade: Trade | None = None
    update: LedgerUpdate | None = None
    action: str = "unchanged"


class OrderReconciler:
    """Synchronises unresolved orders with the exchange, one pass at a time."""

    def __init__(
        self,
        *,
        ledger: PortfolioLedger,
        orders: OrderBook,
        exchange: ExchangeOrderFeed,
        writer: LedgerWriter | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._ledger = ledger
        self._orders = orders
        self._exchange = exchange
        self._writer = writer
        self._clock = clock
        self._logger = get_logger("aegis.order_reconciler")

    def run(self) -> tuple[ReconciliationResult, ...]:
        """Reconcile every open order once; a failure on one never stops the others."""
        results: list[ReconciliationResult] = []
        for order in self._orders.open_orders():
            try:
                state = self._exchange.fetch(order.exchange_order_id, symbol=order.symbol)
            except ExchangeError as exc:
                # Unreachable exchange: keep the order open and try again next pass.
                self._logger.warning(
                    "reconcile_fetch_failed", order_id=order.order_id, error=str(exc)
                )
                continue
            results.append(self._apply(order, state))
        return tuple(results)

    # -------------------------------------------------------------- internals

    def _apply(self, order: OpenOrder, state: ExchangeOrderState) -> ReconciliationResult:
        delta = state.filled_qty - order.filled_qty
        now = self._clock()

        if delta > 0:
            price = state.avg_price
            if price is None or price <= 0:
                self._logger.warning(
                    "reconcile_missing_price", order_id=order.order_id, filled=str(delta)
                )
                return ReconciliationResult(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    previous=order.status,
                    current=state.status,
                    filled_delta=delta,
                    action="skipped_missing_price",
                )
            trade = Trade(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                qty=delta,
                price=price,
                fee=Decimal("0"),
                filled_at=now,
                strategy_id=order.strategy_id,
            )
            update = self._ledger.apply_trade(trade)
            if self._writer is not None:
                self._writer.save_trade(trade)
                self._writer.save_position(update.position)
            self._orders.mark_filled(order, filled_qty=state.filled_qty, avg_price=price)
            self._emit(
                AuditEvent(
                    event_type=EventType.TRADE_RECORDED,
                    entity_type="trade",
                    entity_id=trade.trade_id,
                    actor="order_reconciler",
                    payload={
                        "trade_id": trade.trade_id,
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "side": order.side.value,
                        "qty": str(delta),
                        "price": str(price),
                        "exchange_status": state.status.value,
                        "realized_delta": str(update.realized_delta),
                    },
                )
            )
            self._logger.info(
                "reconcile_fill",
                order_id=order.order_id,
                symbol=order.symbol,
                qty=str(delta),
                price=str(price),
                status=state.status.value,
            )
            closed = state.status is OrderStatus.FILLED
            if closed:
                self._orders.mark_closed(order, state.status)
            return ReconciliationResult(
                order_id=order.order_id,
                symbol=order.symbol,
                previous=order.status,
                current=state.status,
                filled_delta=delta,
                trade=trade,
                update=update,
                action="filled" if closed else "partially_filled",
            )

        if state.status.is_terminal and state.status is not order.status:
            self._orders.mark_closed(order, state.status)
            self._emit(
                AuditEvent(
                    event_type=EventType.ORDER_NOT_FILLED,
                    entity_type="order",
                    entity_id=order.order_id,
                    actor="order_reconciler",
                    payload={
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "reason": f"exchange_{state.status.value.lower()}",
                        "filled_qty": str(state.filled_qty),
                    },
                )
            )
            self._logger.warning(
                "reconcile_closed",
                order_id=order.order_id,
                status=state.status.value,
                released_reservation="none tracked",
            )
            return ReconciliationResult(
                order_id=order.order_id,
                symbol=order.symbol,
                previous=order.status,
                current=state.status,
                filled_delta=Decimal("0"),
                action="closed",
            )

        return ReconciliationResult(
            order_id=order.order_id,
            symbol=order.symbol,
            previous=order.status,
            current=order.status,
            filled_delta=Decimal("0"),
        )

    def _emit(self, event: AuditEvent) -> None:
        if self._writer is not None:
            self._writer.append_audit(event)
