"""add trading.signals and its idempotency key

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16

Signals used to live in process memory, so every restart forgot them and the next scan
of the same closed bar created the idea again. The unique key
``(strategy_id, symbol, timeframe, bar_close_time)`` is the fix: one idea per closed
bar, no matter how many times the process restarts.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("signal_id", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(24), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("strategy_version", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("entry_ref", sa.Numeric(28, 12), nullable=False),
        sa.Column("stop_loss", sa.Numeric(28, 12), nullable=False),
        sa.Column("take_profit", sa.Numeric(28, 12), nullable=False),
        sa.Column("risk_reward", sa.Numeric(28, 12), nullable=False),
        sa.Column("bar_close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("market_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("status_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("signal_id", name="pk_signals"),
        sa.UniqueConstraint(
            "strategy_id", "symbol", "timeframe", "bar_close_time", name="uq_signals_idea_bar"
        ),
        sa.CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_signals_direction_known"),
        sa.CheckConstraint(
            "status IN ('NEW', 'RISK_OK', 'BLOCKED_BY_RISK', 'CONVERTED', 'IGNORED', 'EXPIRED')",
            name="ck_signals_status_known",
        ),
        sa.CheckConstraint("entry_ref > 0", name="ck_signals_entry_ref_positive"),
        sa.CheckConstraint("stop_loss > 0", name="ck_signals_stop_loss_positive"),
        sa.CheckConstraint("take_profit > 0", name="ck_signals_take_profit_positive"),
        sa.CheckConstraint("risk_reward > 0", name="ck_signals_risk_reward_positive"),
        sa.CheckConstraint("expires_at > created_at", name="ck_signals_expiry_after_creation"),
        schema="trading",
    )
    op.create_index(
        "ix_signals_symbol_created_at",
        "signals",
        ["symbol", "created_at"],
        schema="trading",
    )
    op.create_index("ix_signals_status", "signals", ["status"], schema="trading")


def downgrade() -> None:
    op.drop_index("ix_signals_status", "signals", schema="trading")
    op.drop_index("ix_signals_symbol_created_at", "signals", schema="trading")
    op.drop_table("signals", schema="trading")
