"""``trading.bot_activity``: what the bot is doing, in sentences.

A different question from ``audit.audit_logs``. The audit trail is the immutable
evidence of what *happened* to orders and positions; this table is the operator's
radar — "waiting for a candle", "evaluated the market, no crossover", "the gate
refused the order because exposure was 23 %". It is deliberately mutable and short
lived: the writer prunes it, because a feed nobody truncates eventually costs more
than the log file it replaced.

Rows are append-only in practice (nothing updates them), but unlike the audit table
there is no trigger forbidding a delete: retention *is* a delete.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from aegis_db.base import Base

__all__ = ["ACTIVITY_LEVELS", "BotActivity"]

#: The three levels the UI colours. Kept as a CHECK so a typo cannot create a badge
#: the template does not know how to render.
ACTIVITY_LEVELS = ("INFO", "WARNING", "CRITICAL")
_LEVEL_SQL = ", ".join(f"'{level}'" for level in ACTIVITY_LEVELS)


class BotActivity(Base):
    """One line of the activity feed."""

    __tablename__ = "bot_activity"
    __table_args__ = (
        CheckConstraint(f"level IN ({_LEVEL_SQL})", name="ck_bot_activity_level_known"),
        Index("ix_bot_activity_created_at", "created_at"),
        {"schema": "trading"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Stable machine token (``RUNNER_HEARTBEAT``, ``ORDER_REJECTED``, ...) so the
    #: feed can be filtered later without parsing the human sentence.
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(String(8), nullable=False, server_default="INFO")
