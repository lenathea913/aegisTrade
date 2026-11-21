"""add candles.venue to the natural key

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-21

A spot kline and a USDT-M perpetual kline for the same symbol, timeframe and minute are
different prices. Until now both were written under the key ``(environment, symbol,
timeframe, open_time)`` and no column recorded which market produced a bar, so a
backtest could size, fill or exit on the other venue's candle. ``venue`` makes the
series unambiguous and makes the collector's upsert unable to overwrite one with the
other.

``server_default='spot'`` exists so the column can be added to a table with rows in it.
Those rows are then **deleted**, deliberately: they were written by the spot collector
and the futures backfill into the same key, so there is no honest way to label them
after the fact, and a wrong label would be worse than missing data. Re-run
``scripts/backfill_candles.py`` (futures) and/or one collector pass (spot) to refill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KEY: tuple[str, ...] = ("environment", "symbol", "timeframe", "open_time")
NEW_KEY: tuple[str, ...] = ("environment", "venue", "symbol", "timeframe", "open_time")


def upgrade() -> None:
    op.add_column(
        "candles",
        sa.Column("venue", sa.String(8), nullable=False, server_default="spot"),
        schema="market",
    )
    op.drop_constraint("pk_candles", "candles", schema="market", type_="primary")
    op.create_primary_key("pk_candles", "candles", list(NEW_KEY), schema="market")
    op.create_check_constraint(
        "ck_candles_venue_known",
        "candles",
        "venue IN ('spot', 'futures')",
        schema="market",
    )
    # Unlabelled history: collected as spot testnet klines and futures testnet backfills
    # under one key. Provenance is unknowable, so the rows go rather than lying.
    op.execute("DELETE FROM market.candles")


def downgrade() -> None:
    op.drop_constraint("ck_candles_venue_known", "candles", schema="market", type_="check")
    # Collapsing the two markets back into one key can collide. Keep one row per key so
    # the downgrade cannot fail halfway through.
    op.execute(
        "DELETE FROM market.candles a USING market.candles b "
        "WHERE a.environment = b.environment AND a.symbol = b.symbol "
        "AND a.timeframe = b.timeframe AND a.open_time = b.open_time "
        "AND a.venue > b.venue"
    )
    op.drop_constraint("pk_candles", "candles", schema="market", type_="primary")
    op.create_primary_key("pk_candles", "candles", list(OLD_KEY), schema="market")
    op.drop_column("candles", "venue", schema="market")
