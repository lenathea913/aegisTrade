"""record the leverage of positions and trades

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17

Futures mode needs to know, after a restart, at what leverage each leg was opened:
margin and ROE are derived from it. The columns default to 1 so every spot row written
before this migration stays valid and correct.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("leverage", sa.Integer(), nullable=False, server_default="1"),
        schema="trading",
    )
    op.add_column(
        "trades",
        sa.Column("leverage", sa.Integer(), nullable=False, server_default="1"),
        schema="trading",
    )
    op.create_check_constraint(
        "ck_positions_leverage_range",
        "positions",
        "leverage >= 1 AND leverage <= 5",
        schema="trading",
    )
    op.create_check_constraint(
        "ck_trades_leverage_range",
        "trades",
        "leverage >= 1 AND leverage <= 5",
        schema="trading",
    )


def downgrade() -> None:
    op.drop_constraint("ck_trades_leverage_range", "trades", schema="trading", type_="check")
    op.drop_constraint(
        "ck_positions_leverage_range", "positions", schema="trading", type_="check"
    )
    op.drop_column("trades", "leverage", schema="trading")
    op.drop_column("positions", "leverage", schema="trading")
