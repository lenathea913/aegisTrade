"""Idempotent upsert statement and the write wrapper (no database needed)."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import create_mock_engine
from sqlalchemy.engine import Dialect
from sqlalchemy.engine.mock import MockConnection
from sqlalchemy.orm import Session

from aegis_core import Candle, Timeframe
from market_collector.store import CandleStore, build_upsert, candle_rows

START = datetime(2024, 1, 1, tzinfo=UTC)


def postgres_dialect() -> Dialect:
    """A real PostgreSQL dialect without a server: SQLAlchemy's mock engine."""
    engine: MockConnection = create_mock_engine(
        "postgresql+psycopg://", lambda *args, **kwargs: None
    )
    return engine.dialect


def make_candle(hour: int = 0, *, close: str = "100") -> Candle:
    open_time = START + timedelta(hours=hour)
    return Candle(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        open_time=open_time,
        close_time=open_time + timedelta(hours=1),
        open=Decimal(close),
        high=Decimal(close) + 1,
        low=Decimal(close) - 1,
        close=Decimal(close),
        volume=Decimal("5"),
        trades=7,
    )


class _Result:
    """Cursor stand-in: the store counts rows returned by ``RETURNING``."""

    def __init__(self, returned: int) -> None:
        self.returned = returned
        # The real driver reports -1 for these multi-row inserts; the store must not
        # trust it. Keeping it here makes that regression impossible to reintroduce.
        self.rowcount = -1

    def all(self) -> list[tuple[int]]:
        return [(index,) for index in range(self.returned)]


class RecordingSession:
    def __init__(self, returned: int = 1) -> None:
        self.statements: list[Any] = []
        self.savepoints = 0
        self.returned = returned

    def begin_nested(self) -> contextlib.AbstractContextManager[None]:
        self.savepoints += 1
        return contextlib.nullcontext()

    def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.returned)


def test_rows_carry_exchange_source_and_domain_values() -> None:
    rows = candle_rows([make_candle()], exchange="binance", source="csv")

    assert rows[0]["exchange"] == "binance"
    assert rows[0]["source"] == "csv"
    assert rows[0]["timeframe"] == "1h"
    assert rows[0]["trades"] == 7


def test_duplicate_bars_in_one_batch_are_collapsed() -> None:
    rows = candle_rows(
        [make_candle(0), make_candle(0), make_candle(1)], exchange="binance", source="csv"
    )

    assert len(rows) == 2


def test_upsert_uses_the_natural_key_and_do_nothing() -> None:
    statement = build_upsert([make_candle()], exchange="binance", source="csv")
    sql = str(statement.compile(dialect=postgres_dialect()))

    assert "INSERT INTO market.candles" in sql
    assert "ON CONFLICT (symbol, timeframe, open_time) DO NOTHING" in sql


def test_upsert_counts_rows_returned_by_the_statement() -> None:
    session = RecordingSession(returned=2)
    store = CandleStore(cast(Session, session))

    inserted = store.upsert([make_candle(0), make_candle(1)], exchange="binance", source="csv")

    assert inserted == 2
    assert len(session.statements) == 1
    assert "RETURNING" in str(session.statements[0].compile(dialect=postgres_dialect()))


def test_upsert_runs_inside_a_savepoint() -> None:
    session = RecordingSession(returned=1)

    CandleStore(cast(Session, session)).upsert([make_candle()], exchange="binance", source="csv")

    assert session.savepoints == 1


def test_empty_batch_skips_the_database() -> None:
    session = RecordingSession()
    store = CandleStore(cast(Session, session))

    assert store.upsert([], exchange="binance", source="csv") == 0
    assert session.statements == []


def test_conflicting_batch_inserts_nothing_and_counts_zero() -> None:
    """Every row already exists: ``RETURNING`` is empty, so the count is zero."""
    session = RecordingSession(returned=0)

    inserted = CandleStore(cast(Session, session)).upsert(
        [make_candle()], exchange="binance", source="csv"
    )

    assert inserted == 0
