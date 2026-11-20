"""add the trading.bot_activity feed

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-20

The runner already says what it is doing — but only to the process log, which means the
operator has to tail PowerShell to answer "is the bot alive, and why did it not trade?".
This table is the same information as rows the dashboard can poll.

The feed is intentionally not the audit log: audit rows are immutable evidence, while
these are disposable telemetry with a retention policy (the writer keeps the newest
1 000 rows and drops anything older than a day). That is why there is no
``append-only`` trigger here.

The index is on ``created_at`` because every read is "the newest N, newest first".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_activity",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False, server_default="INFO"),
        sa.CheckConstraint(
            "level IN ('INFO', 'WARNING', 'CRITICAL')",
            name="ck_bot_activity_level_known",
        ),
        schema="trading",
    )
    op.create_index(
        "ix_bot_activity_created_at",
        "bot_activity",
        ["created_at"],
        schema="trading",
    )


def downgrade() -> None:
    op.drop_index("ix_bot_activity_created_at", table_name="bot_activity", schema="trading")
    op.drop_table("bot_activity", schema="trading")
