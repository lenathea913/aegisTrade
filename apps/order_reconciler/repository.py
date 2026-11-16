"""Database side of the reconciler: open orders and their status updates.

The book accepts either a session factory (each call gets its own short transaction) or
an existing ``Session``. The CLI passes the session that also holds the ledger writer,
so a reconciled trade, its position upsert, its audit row and the order's new status
commit — or roll back — together.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from aegis_core import OrderStatus, Side
from aegis_db import OrderRecord, session_scope
from order_reconciler.reconciler import OpenOrder

__all__ = ["SqlOrderBook"]

#: Statuses the exchange may still change. Paper orders never appear here: they are
#: inserted as filled/not-filled and carry no exchange_order_id.
_OPEN_STATUSES = (
    OrderStatus.NEW.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)


class SqlOrderBook:
    """Reads unresolved orders and writes their new status."""

    def __init__(self, source: sessionmaker[Session] | Session) -> None:
        self._source = source

    @contextmanager
    def _scope(self) -> Iterator[Session]:
        if isinstance(self._source, Session):
            yield self._source
            return
        with session_scope(self._source) as session:
            yield session

    def open_orders(self) -> Sequence[OpenOrder]:
        with self._scope() as session:
            rows = (
                session.execute(
                    select(OrderRecord)
                    .where(
                        OrderRecord.status.in_(_OPEN_STATUSES),
                        OrderRecord.exchange_order_id.is_not(None),
                        OrderRecord.broker != "paper",
                    )
                    .order_by(OrderRecord.created_at)
                )
                .scalars()
                .all()
            )
        return tuple(
            OpenOrder(
                order_id=row.order_id,
                exchange_order_id=str(row.exchange_order_id),
                symbol=row.symbol,
                side=Side(row.side),
                qty=row.qty,
                filled_qty=row.filled_qty,
                status=OrderStatus(row.status),
                strategy_id=row.strategy_id,
            )
            for row in rows
        )

    def mark_filled(self, order: OpenOrder, *, filled_qty: Decimal, avg_price: Decimal) -> None:
        status = (
            OrderStatus.FILLED.value
            if filled_qty >= order.qty
            else OrderStatus.PARTIALLY_FILLED.value
        )
        with self._scope() as session:
            session.execute(
                update(OrderRecord)
                .where(OrderRecord.order_id == order.order_id)
                .values(status=status, filled_qty=filled_qty, avg_fill_price=avg_price)
            )

    def mark_closed(self, order: OpenOrder, status: OrderStatus) -> None:
        with self._scope() as session:
            session.execute(
                update(OrderRecord)
                .where(OrderRecord.order_id == order.order_id)
                .values(status=status.value)
            )
