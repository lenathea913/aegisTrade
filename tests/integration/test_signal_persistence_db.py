"""Signal persistence against a real PostgreSQL: restarts must not duplicate ideas.

Skipped unless ``AEGIS_TEST_DATABASE_URL`` points at a database the suite may write to.
The property under test cannot be faked: the same closed bar scanned twice — the
restart case — leaves exactly one row, and a status the operator already changed stays
changed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aegis_core import Signal, SignalDirection, SignalStatus, Timeframe
from aegis_db import Base, SignalRecord, create_session_factory
from signal_service.store import SqlSignalStore

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("AEGIS_TEST_DATABASE_URL")
BAR_CLOSE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    if not DATABASE_URL:
        pytest.skip("AEGIS_TEST_DATABASE_URL is not set")
    engine: Engine = create_engine(DATABASE_URL)
    with engine.begin() as connection:
        for schema in ("market", "trading", "audit"):
            connection.execute(text(f"create schema if not exists {schema}"))
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("delete from trading.signals"))
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def make_signal(created_at: datetime | None = None) -> Signal:
    created = created_at or BAR_CLOSE
    return Signal(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        strategy_id="simple_ema_cross",
        strategy_version="1",
        direction=SignalDirection.LONG,
        entry_ref=Decimal("30000"),
        stop_loss=Decimal("29500"),
        take_profit=Decimal("31000"),
        risk_reward=Decimal("2"),
        reason_codes=("ema_cross",),
        market_context={"bars": 120, "quote_as_of": (BAR_CLOSE - timedelta(hours=1)).isoformat()},
        created_at=created,
        expires_at=created + timedelta(hours=1),
        status=SignalStatus.RISK_OK,
    )


def _count(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return int(session.execute(select(func.count()).select_from(SignalRecord)).scalar_one())


def test_the_same_bar_is_never_stored_twice(factory: sessionmaker[Session]) -> None:
    store = SqlSignalStore(factory)
    signal = make_signal()

    store.add(signal)
    # A restarted process rescans the same closed bar.
    store.add(make_signal(created_at=BAR_CLOSE + timedelta(minutes=5)))

    assert _count(factory) == 1


def test_a_restart_does_not_reset_an_operator_decision(factory: sessionmaker[Session]) -> None:
    store = SqlSignalStore(factory)
    signal = make_signal()
    store.add(signal)
    store.update(signal.with_status(SignalStatus.CONVERTED))

    store.add(make_signal())

    assert _count(factory) == 1
    stored = store.get(signal.signal_id)
    assert stored is not None
    assert stored.status is SignalStatus.CONVERTED


def test_stored_signals_are_listed_newest_first(factory: sessionmaker[Session]) -> None:
    store = SqlSignalStore(factory)
    older = make_signal()
    newer = make_signal(created_at=BAR_CLOSE + timedelta(hours=1)).model_copy()
    newer = newer.model_copy(
        update={
            "market_context": {
                "bars": 120,
                "quote_as_of": BAR_CLOSE.isoformat(),
            },
            "expires_at": BAR_CLOSE + timedelta(hours=2),
        }
    )

    store.add(older)
    store.add(newer)

    listed = store.list()

    assert [item.signal_id for item in listed] == [newer.signal_id, older.signal_id]
    assert listed[0].entry_ref == Decimal("30000")


def test_updating_an_unknown_signal_is_an_error(factory: sessionmaker[Session]) -> None:
    store = SqlSignalStore(factory)

    with pytest.raises(KeyError):
        store.update(make_signal())
