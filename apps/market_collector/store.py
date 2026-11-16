"""Idempotent persistence into ``market.candles``.

Idempotency comes from the table's natural primary key
``(environment, symbol, timeframe, open_time)`` plus ``ON CONFLICT DO NOTHING``:
re-collecting an overlapping window is free and can never create a duplicate bar.
Re-running the collector is therefore always safe, which is what makes restarts and
retries cheap. ``environment`` is written with every row, so testnet bars and mainnet
bars are separate series in the same table instead of silently mixing.

The inserted count comes from ``RETURNING``, not ``rowcount``: psycopg reports
``rowcount = -1`` for the multi-row INSERT SQLAlchemy emits here, so counting a
cursor attribute silently reported "0 inserted" even when every row was new (found
against a real PostgreSQL 16 instance, not against fakes).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, cast

from sqlalchemy import Insert, Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from aegis_core import Candle
from aegis_db import Candle as CandleRow

__all__ = ["CandleStore", "CandleWriter", "build_upsert", "candle_rows"]


class CandleWriter(Protocol):
    """Anything the collector can persist candles through (fakeable in tests)."""

    def upsert(
        self, candles: Sequence[Candle], *, exchange: str, source: str, environment: str
    ) -> int:
        """Insert candles idempotently and return how many rows were new."""


_CONFLICT_KEY = ("environment", "symbol", "timeframe", "open_time")


def candle_rows(
    candles: Sequence[Candle], *, exchange: str, source: str, environment: str
) -> list[dict[str, Any]]:
    """Map domain candles to row dicts, de-duplicated by the natural key."""
    rows: dict[tuple[str, str, str, Any], dict[str, Any]] = {}
    for candle in candles:
        key = (environment, candle.symbol, candle.timeframe.value, candle.open_time)
        rows[key] = {
            "exchange": exchange,
            "environment": environment,
            "symbol": candle.symbol,
            "timeframe": candle.timeframe.value,
            "open_time": candle.open_time,
            "close_time": candle.close_time,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "trades": candle.trades,
            "source": source,
        }
    return list(rows.values())


def build_upsert(
    candles: Sequence[Candle], *, exchange: str, source: str, environment: str
) -> Insert:
    """Build the ``INSERT ... ON CONFLICT DO NOTHING`` statement for one batch."""
    table = cast(Table, CandleRow.__table__)
    return (
        pg_insert(table)
        .values(candle_rows(candles, exchange=exchange, source=source, environment=environment))
        .on_conflict_do_nothing(index_elements=list(_CONFLICT_KEY))
    )


class CandleStore:
    """Thin write layer; the caller owns the transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self, candles: Sequence[Candle], *, exchange: str, source: str, environment: str
    ) -> int:
        """Insert new bars, ignore existing ones, and return the inserted count."""
        if not candles:
            return 0
        table = cast(Table, CandleRow.__table__)
        statement = build_upsert(
            candles, exchange=exchange, source=source, environment=environment
        ).returning(table.c.open_time)
        # A SAVEPOINT keeps one bad batch from poisoning the caller's transaction,
        # so the remaining series of a pass still collect normally. RETURNING rows
        # are exactly the rows this statement inserted: conflicting ones are skipped.
        with self._session.begin_nested():
            result = cast(
                CursorResult[Any],
                self._session.execute(statement),
            )
            return len(result.all())
