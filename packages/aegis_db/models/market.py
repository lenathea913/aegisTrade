"""``market.candles``: OHLCV storage.

The primary key is the natural key ``(environment, venue, symbol, timeframe,
open_time)``. That is what makes the collector idempotent: re-collecting the same
candle is an ``ON CONFLICT DO NOTHING/UPDATE`` instead of a duplicate row, and no
surrogate id can drift.

Both parts of the venue belong in the key:

* ``environment`` (``testnet``/``mainnet``) — testnet prices collected while the
  platform is paper-only can never be read back as mainnet history after a switch to
  real money.
* ``venue`` (``spot``/``futures``) — a spot kline and a USDT-M perpetual kline for the
  same minute are **different prices**. Before this column existed the futures
  backfill and the spot collector wrote into one series, so a backtest could take its
  execution bar from the other market. Every reader filters on the venue configured in
  ``execution.market_type``, and the collector labels the rows it writes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from aegis_core.enums import Timeframe
from aegis_db.base import Base

_TIMEFRAME_SQL = ", ".join(f"'{timeframe.value}'" for timeframe in Timeframe)

__all__ = ["Candle"]


class Candle(Base):
    """One OHLCV candle for one symbol/timeframe."""

    __tablename__ = "candles"
    __table_args__ = (
        CheckConstraint(f"timeframe IN ({_TIMEFRAME_SQL})", name="ck_candles_timeframe_known"),
        CheckConstraint("open_time < close_time", name="ck_candles_open_before_close"),
        CheckConstraint(
            "high >= GREATEST(open, close) AND low <= LEAST(open, close)",
            name="ck_candles_ohlc_bounds",
        ),
        CheckConstraint("low > 0", name="ck_candles_low_positive"),
        CheckConstraint("volume >= 0", name="ck_candles_volume_non_negative"),
        CheckConstraint(
            "environment IN ('testnet', 'mainnet')",
            name="ck_candles_environment_known",
        ),
        CheckConstraint("venue IN ('spot', 'futures')", name="ck_candles_venue_known"),
        {"schema": "market"},
    )

    exchange: Mapped[str] = mapped_column(String(32), nullable=False, server_default="binance")
    #: ``testnet`` or ``mainnet``; part of the primary key so venues never mix.
    environment: Mapped[str] = mapped_column(String(8), primary_key=True, server_default="testnet")
    #: ``spot`` or ``futures``; part of the key so the two markets never share a bar.
    venue: Mapped[str] = mapped_column(String(8), primary_key=True, server_default="spot")
    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    open: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)

    trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="csv")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
