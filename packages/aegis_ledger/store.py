"""Persistence for the ledger, the gateway's orders and the audit trail.

The ledger computes; this module only writes what it computed, in the caller's
transaction. Orders and trades are inserts (a trade belongs to one fill and is never
rewritten); positions are upserted on the symbol key because they are current state.

The ``LedgerWriter`` protocol is what the pipeline depends on, so tests can record
writes without a database.
"""

from __future__ import annotations

from typing import Protocol, cast

from sqlalchemy import Insert, Table, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from aegis_core import ApprovedOrder, Order, Position, Trade
from aegis_db import AuditLog, OrderRecord, PositionRecord, TradeRecord
from aegis_ledger.audit import AuditEvent
from aegis_ledger.records import audit_row, order_row, position_row, trade_row

__all__ = ["LedgerStore", "LedgerWriter", "build_position_upsert"]

_POSITION_UPDATE_COLUMNS = (
    "qty",
    "avg_entry",
    "unrealized_pnl",
    "realized_pnl",
    "opened_at",
    "leverage",
    # Protective levels live on the row so a restart can rebuild the monitor's stops.
    "stop_loss",
    "take_profit",
)


class LedgerWriter(Protocol):
    """The write surface the trading pipeline needs."""

    def save_order(self, order: Order, approved: ApprovedOrder) -> None:
        """Persist one order together with the approval that allowed it."""

    def save_trade(self, trade: Trade) -> None:
        """Persist one executed trade."""

    def save_position(self, position: Position) -> None:
        """Upsert the current position for the symbol."""

    def append_audit(self, event: AuditEvent) -> None:
        """Append one audit event."""


def build_position_upsert(position: Position) -> Insert:
    """``INSERT ... ON CONFLICT (symbol) DO UPDATE`` for current position state."""
    table = cast(Table, PositionRecord.__table__)
    row = position_row(position)
    return (
        pg_insert(table)
        .values(**row)
        .on_conflict_do_update(
            index_elements=["symbol"],
            set_={column: row[column] for column in _POSITION_UPDATE_COLUMNS},
        )
    )


class LedgerStore:
    """SQLAlchemy writer; the caller owns the transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save_order(self, order: Order, approved: ApprovedOrder) -> None:
        table = cast(Table, OrderRecord.__table__)
        self._session.execute(insert(table).values(**order_row(order, approved)))

    def save_trade(self, trade: Trade) -> None:
        table = cast(Table, TradeRecord.__table__)
        self._session.execute(insert(table).values(**trade_row(trade)))

    def save_position(self, position: Position) -> None:
        self._session.execute(build_position_upsert(position))

    def append_audit(self, event: AuditEvent) -> None:
        table = cast(Table, AuditLog.__table__)
        self._session.execute(insert(table).values(**audit_row(event)))
