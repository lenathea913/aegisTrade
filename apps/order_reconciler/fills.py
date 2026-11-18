"""Durable booking of protective exits the venue executed on our behalf.

A stop that fires on the exchange changes the account immediately and our book not at
all. The reconciler explains the gap with the venue's own fills (``VenueFillFeed``);
this module is where that explanation becomes a record: an order row, the trades, the
resulting position and the audit events, in one transaction.

Two details matter for correctness:

* **The order row is real, in a limited sense.** ``trading.trades.order_id`` is a
  foreign key, so a venue-originated fill needs a row to point at. It is written with
  ``broker='venue'`` and ``strategy_id='venue_protection'`` and never passes through the
  risk gate — the venue already executed it. Nothing reads it as an approval.
* **Everything is idempotent.** Trade ids derive from the venue order id, and all
  inserts are ``ON CONFLICT DO NOTHING``: a second pass after a crash re-derives the
  same rows instead of doubling the journal.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from aegis_core import OrderStatus, OrderType, Position, Trade
from aegis_db import OrderRecord, TradeRecord, session_scope
from aegis_ledger import AuditEvent, EventType, LedgerStore
from aegis_ledger.records import trade_row
from aegis_observability import get_logger
from order_reconciler.positions import PROTECTION_STRATEGY_ID

__all__ = ["SqlVenueFillRecorder"]


class SqlVenueFillRecorder:
    """Writes the trades that explained a drift, and the book they produced."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._logger = get_logger("aegis.position_reconciler")

    def record_venue_fills(
        self, *, symbol: str, trades: Sequence[Trade], position: Position
    ) -> None:
        """Persist ``trades`` and ``position`` together, then audit both."""
        if not trades:
            return
        with session_scope(self._factory) as session:
            store = LedgerStore(session)
            for trade in trades:
                self._insert_venue_order(session, trade)
                # save_trade is an insert; the deterministic id makes a replay a no-op.
                table = cast(Table, TradeRecord.__table__)
                session.execute(
                    pg_insert(table).values(**trade_row(trade)).on_conflict_do_nothing()
                )
                store.append_audit(
                    AuditEvent(
                        event_type=EventType.TRADE_RECORDED,
                        entity_type="trade",
                        entity_id=trade.trade_id,
                        actor="position_reconciler",
                        payload={
                            "trade_id": trade.trade_id,
                            "order_id": trade.order_id,
                            "symbol": trade.symbol,
                            "side": trade.side.value,
                            "qty": str(trade.qty),
                            "price": str(trade.price),
                            "fee": str(trade.fee),
                            "source": PROTECTION_STRATEGY_ID,
                            "detail": "venue stop/target execution booked during reconciliation",
                        },
                    )
                )
            store.save_position(position)
            store.append_audit(
                AuditEvent(
                    event_type=EventType.POSITION_UPDATED,
                    entity_type="position",
                    entity_id=position.symbol,
                    actor="position_reconciler",
                    payload={
                        "symbol": position.symbol,
                        "qty": str(position.qty),
                        "avg_entry": str(position.avg_entry),
                        "realized_pnl": str(position.realized_pnl),
                        "source": PROTECTION_STRATEGY_ID,
                    },
                )
            )
        self._logger.info(
            "venue_fills_recorded",
            symbol=symbol,
            trades=len(trades),
            position_qty=str(position.qty),
        )

    def _insert_venue_order(self, session: Session, trade: Trade) -> None:
        """The ``trading.orders`` row the trade's foreign key needs.

        ``stop_loss`` carries the fill price because the column is NOT NULL and the
        trigger price is not part of a ``userTrades`` row; the fill is the closest
        evidence we have of where the exit actually happened.
        """
        venue_order_id = trade.order_id.split(":", 1)[-1]
        table = cast(Table, OrderRecord.__table__)
        row: dict[str, Any] = {
            "order_id": trade.order_id,
            "approval_id": PROTECTION_STRATEGY_ID,
            "symbol": trade.symbol,
            "side": trade.side.value,
            "order_type": OrderType.MARKET.value,
            "qty": trade.qty,
            "price": None,
            "stop_loss": trade.price,
            "take_profit": None,
            "status": OrderStatus.FILLED.value,
            "broker": "venue",
            "exchange_order_id": venue_order_id,
            "filled_qty": trade.qty,
            "avg_fill_price": trade.price,
            "strategy_id": PROTECTION_STRATEGY_ID,
            "reason": "protective exit executed on the venue for an open position",
            "max_risk_amount": trade.qty * trade.price,
            "risk_checks": [PROTECTION_STRATEGY_ID],
            "approved_at": trade.filled_at,
        }
        session.execute(pg_insert(table).values(**row).on_conflict_do_nothing())
