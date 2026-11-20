"""AegisTrade persistence layer (SQLAlchemy 2.0 + Alembic).

Schemas: ``market`` (candles), ``trading`` (orders, positions, trades) and
``audit`` (append-only ``audit_logs``).
"""

from __future__ import annotations

from aegis_db.activity import (
    ActivityReader,
    ActivityRecord,
    ActivityWriter,
    NullActivityWriter,
    RecordingActivityWriter,
    SqlActivityReader,
    SqlActivityWriter,
    prune_activity,
)
from aegis_db.base import NAMING_CONVENTION, Base, TimestampMixin
from aegis_db.models import (
    AuditLog,
    BotActivity,
    Candle,
    OrderRecord,
    PositionRecord,
    ReconciliationStateRecord,
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
    "ActivityReader",
    "ActivityRecord",
    "ActivityWriter",
    "AuditLog",
    "Base",
    "BotActivity",
    "Candle",
    "NullActivityWriter",
    "OrderRecord",
    "PositionRecord",
    "ReconciliationStateRecord",
    "RecordingActivityWriter",
    "SignalRecord",
    "SqlActivityReader",
    "SqlActivityWriter",
    "TimestampMixin",
    "TradeRecord",
    "create_engine_from_settings",
    "create_session_factory",
    "prune_activity",
    "session_scope",
]
