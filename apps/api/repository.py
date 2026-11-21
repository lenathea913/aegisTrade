"""Read model for the API.

The API is read-only in this phase: it never places an order. It reads executed
trades and audit events from PostgreSQL and reports them, and it **rebuilds the book
from those rows on every read**: the runner is a different process writing to the same
tables, so an in-memory ledger reconstructed only at startup describes whatever the API
saw when it booted, not what the bot currently holds.

``TradingRepository`` is a protocol so route and service tests need no database.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from aegis_core import Trade
from aegis_db import AuditLog, TradeRecord, session_scope
from aegis_ledger import AuditEvent, EventType, PortfolioLedger, trade_from_row

__all__ = [
    "ApiRepository",
    "SqlTradingRepository",
    "audit_event_from_row",
    "trade_from_row",
]


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

    def reconstruct(self, ledger: PortfolioLedger, *, strict: bool = True) -> None:
        """Reload ``ledger`` from ``trading.trades`` and ``trading.positions``."""

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

    def reconstruct(self, ledger: PortfolioLedger, *, strict: bool = True) -> None:
        """Replay the durable record into ``ledger`` in one short session."""
        with session_scope(self._factory) as session:
            ledger.reconstruct_from_db(session, strict=strict)

    def audit_events(self, limit: int) -> Sequence[AuditEvent]:
        with session_scope(self._factory) as session:
            rows = session.execute(
                select(AuditLog)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(limit)
            ).scalars()
            return [audit_event_from_row(row) for row in rows]
