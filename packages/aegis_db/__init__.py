"""AegisTrade persistence layer (SQLAlchemy 2.0 + Alembic).

Schemas: ``market`` (candles), ``trading`` (orders, positions, trades) and
``audit`` (append-only ``audit_logs``).
"""

from __future__ import annotations

from aegis_db.base import NAMING_CONVENTION, Base, TimestampMixin
from aegis_db.models import (
    AuditLog,
    Candle,
    OrderRecord,
    PositionRecord,
    SignalRecord,
    TradeRecord,
)
from aegis_db.session import (
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)

__all__ = [
    "NAMING_CONVENTION",
    "AuditLog",
    "Base",
    "Candle",
    "OrderRecord",
    "PositionRecord",
    "SignalRecord",
    "TimestampMixin",
    "TradeRecord",
    "create_engine_from_settings",
    "create_session_factory",
    "session_scope",
]
