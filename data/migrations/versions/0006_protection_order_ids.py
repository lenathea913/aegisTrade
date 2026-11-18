"""remember the venue order ids of the protective exits

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18

Revision 0005 persisted the *levels* of a stop, which is enough for the in-process
monitor but not for the venue: after a restart we knew a stop should exist, not
whether one did. A stop cancelled by hand in the exchange UI looked identical to a
working one.

``stop_order_id`` / ``tp_order_id`` close that gap. On startup the runner asks the
venue whether the recorded stop is still open and re-arms it when it is not, and
``/positions`` can show whether a position is actually covered instead of assuming it.

The CHECK keeps the same invariant as the levels: a flat row can reference nothing,
because there is no exposure for an exit to close.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("stop_order_id", sa.String(length=64), nullable=True),
        schema="trading",
    )
    op.add_column(
        "positions",
        sa.Column("tp_order_id", sa.String(length=64), nullable=True),
        schema="trading",
    )
    op.create_check_constraint(
        "ck_positions_protection_orders_match_exposure",
        "positions",
        "(qty = 0 AND stop_order_id IS NULL AND tp_order_id IS NULL) OR "
        "(qty <> 0 AND (stop_order_id IS NULL OR stop_loss IS NOT NULL) "
        "AND (tp_order_id IS NULL OR take_profit IS NOT NULL))",
        schema="trading",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_positions_protection_orders_match_exposure",
        "positions",
        schema="trading",
        type_="check",
    )
    op.drop_column("positions", "tp_order_id", schema="trading")
    op.drop_column("positions", "stop_order_id", schema="trading")
