"""persist protective levels and the reconciliation state

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17

Two gaps closed together, because they fail in the same way — silently.

1. A stop lived only in the ledger's memory, so a restart left the position open and
   unmonitored. ``trading.positions`` now carries the stop and target of the open leg;
   a flat row may not keep either (CHECK), so the levels cannot outlive the exposure.
2. Nothing compared the book with the venue, so a missed fill could sit undetected
   forever. ``trading.reconciliation_state`` is the singleton a reconciler writes and
   the runner reads before any entry: drift halts entries until an operator clears it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("stop_loss", sa.Numeric(28, 12), nullable=True),
        schema="trading",
    )
    op.add_column(
        "positions",
        sa.Column("take_profit", sa.Numeric(28, 12), nullable=True),
        schema="trading",
    )
    op.create_check_constraint(
        "ck_positions_protection_matches_exposure",
        "positions",
        "(qty = 0 AND stop_loss IS NULL AND take_profit IS NULL) OR "
        "(qty <> 0 AND (stop_loss IS NULL OR stop_loss > 0) "
        "AND (take_profit IS NULL OR take_profit > 0))",
        schema="trading",
    )
    op.create_table(
        "reconciliation_state",
        sa.Column("id", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="unchecked", nullable=False),
        sa.Column("entries_halted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("drift_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_reconciliation_state_singleton"),
        sa.CheckConstraint(
            "status IN ('synced', 'drift', 'unchecked')",
            name="ck_reconciliation_state_status_known",
        ),
        sa.CheckConstraint("drift_count >= 0", name="ck_reconciliation_state_count_non_negative"),
        sa.PrimaryKeyConstraint("id"),
        schema="trading",
    )


def downgrade() -> None:
    op.drop_table("reconciliation_state", schema="trading")
    op.drop_constraint(
        "ck_positions_protection_matches_exposure",
        "positions",
        schema="trading",
        type_="check",
    )
    op.drop_column("positions", "take_profit", schema="trading")
    op.drop_column("positions", "stop_loss", schema="trading")
