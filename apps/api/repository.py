"""Read model for the API.

The API is read-only in this phase: it never places an order. It reads executed
trades and audit events from PostgreSQL and reports them; the in-process ledger
supplies positions, equity and loss windows, because it is the source of truth
while the service runs.

``TradingRepository`` is a protocol so route and service tests need no database.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from aegis_core import Side, Trade
from aegis_db import AuditLog, TradeRecord, session_scope
from aegis_ledger import AuditEvent, EventType

__all__ = [
    "ApiRepository",
    "SqlTradingRepository",
    "audit_event_from_row",
    "trade_from_row",
]


def trade_from_row(row: TradeRecord) -> Trade:
    """Map a ``trading.trades`` row back into the domain."""
    return Trade(
        trade_id=row.trade_id,
        order_id=row.order_id,
        symbol=row.symbol,
        side=Side(row.side),
        qty=row.qty,
        price=row.price,
        fee=row.fee,
        funding=row.funding,
        filled_at=row.filled_at,
        strategy_id=row.strategy_id,
        r_multiple=row.r_multiple,
    )


def audit_event_from_row(row: AuditLog) -> AuditEvent:
    """Map an ``audit.audit_logs`` row back into the domain."""
    return AuditEvent(
        event_type=EventType(row.event_type),
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        actor=row.actor,
        payload=row.payload,
        created_at=row.created_at,
    )


class ApiRepository(Protocol):
    """The read surface the service needs."""

    def ping(self) -> bool:
        """Whether the database answers."""

    def trades(self, limit: int) -> Sequence[Trade]:
        """Most recent executed trades, newest first."""

    def audit_events(self, limit: int) -> Sequence[AuditEvent]:
        """Most recent audit events, newest first."""


class SqlTradingRepository:
    """PostgreSQL implementation. One short session per call."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def ping(self) -> bool:
        try:
            with session_scope(self._factory) as session:
                session.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def trades(self, limit: int) -> Sequence[Trade]:
        with session_scope(self._factory) as session:
            rows = session.execute(
                select(TradeRecord).order_by(TradeRecord.filled_at.desc()).limit(limit)
            ).scalars()
            return [trade_from_row(row) for row in rows]

    def audit_events(self, limit: int) -> Sequence[AuditEvent]:
        with session_scope(self._factory) as session:
            rows = session.execute(
                select(AuditLog)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(limit)
            ).scalars()
            return [audit_event_from_row(row) for row in rows]
