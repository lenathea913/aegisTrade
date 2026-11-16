"""add candles.environment to the natural key

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16

Testnet and mainnet candles must never share a series. The column is added with
``server_default='testnet'`` so existing rows stay readable: everything collected so
far came from the testnet venue (or from CSV seeds written while testnet was the only
configured venue). After the backfill it becomes part of the primary key.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KEY: tuple[str, ...] = ("symbol", "timeframe", "open_time")
NEW_KEY: tuple[str, ...] = ("environment", "symbol", "timeframe", "open_time")


def upgrade() -> None:
    op.add_column(
        "candles",
        sa.Column(
            "environment", sa.String(8), nullable=False, server_default="testnet"
        ),
        schema="market",
    )
    op.drop_constraint("pk_candles", "candles", schema="market", type_="primary")
    op.create_primary_key("pk_candles", "candles", list(NEW_KEY), schema="market")
    op.create_check_constraint(
        "ck_candles_environment_known",
        "candles",
        "environment IN ('testnet', 'mainnet')",
        schema="market",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_candles_environment_known", "candles", schema="market", type_="check"
    )
    op.drop_constraint("pk_candles", "candles", schema="market", type_="primary")
    op.create_primary_key("pk_candles", "candles", list(OLD_KEY), schema="market")
    op.drop_column("candles", "environment", schema="market")
