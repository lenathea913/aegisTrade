"""Ledger reconstruction against a real PostgreSQL.

Skipped unless ``AEGIS_TEST_DATABASE_URL`` points at a database the suite may write
to (CI provides one). It proves the thing fakes cannot: the rows written by the
pipeline are enough to rebuild the book after a restart.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aegis_config import load_settings
from aegis_core import OrderStatus, OrderType, Side, Trade
from aegis_db import Base, OrderRecord, create_session_factory
from aegis_ledger import LedgerStore, PortfolioLedger
from api.app import _restore_ledger
from api.repository import SqlTradingRepository
from api.service import ApiService

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("AEGIS_TEST_DATABASE_URL")
SYMBOL = "BTC/USDT"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


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
        connection.execute(text("delete from trading.trades"))
        connection.execute(text("delete from trading.positions"))
        connection.execute(text("delete from trading.orders"))
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def _persist(session: Session, ledger: PortfolioLedger, trade: Trade) -> None:
    """Write the order, the trade and the resulting position, in FK order."""
    session.add(
        OrderRecord(
            order_id=trade.order_id,
            approval_id=f"appr_{trade.order_id}",
            symbol=trade.symbol,
            side=trade.side.value,
            order_type=OrderType.MARKET.value,
            qty=trade.qty,
            price=trade.price,
            stop_loss=trade.price,
            take_profit=None,
            status=OrderStatus.FILLED.value,
            broker="paper",
            filled_qty=trade.qty,
            avg_fill_price=trade.price,
            strategy_id=trade.strategy_id,
            reason="ledger reconstruction integration test",
            max_risk_amount=Decimal("1"),
            risk_checks=[],
            approved_at=trade.filled_at,
        )
    )
    session.flush()
    store = LedgerStore(session)
    update = ledger.apply_trade(trade)
    store.save_trade(trade)
    store.save_position(update.position)


def _write_round_trip(factory: sessionmaker[Session]) -> None:
    """Buy then sell 0.1 BTC, exactly as the pipeline would persist it."""
    entry = Trade(
        order_id="ord_reconstruct_1",
        symbol=SYMBOL,
        side=Side.BUY,
        qty=Decimal("0.1"),
        price=Decimal("30000"),
        fee=Decimal("1.5"),
        filled_at=NOW,
        strategy_id="simple_ema_cross",
    )
    exit_ = Trade(
        order_id="ord_reconstruct_2",
        symbol=SYMBOL,
        side=Side.SELL,
        qty=Decimal("0.1"),
        price=Decimal("31000"),
        fee=Decimal("1.55"),
        filled_at=NOW + timedelta(hours=1),
        strategy_id="simple_ema_cross",
    )
    with factory() as session:
        ledger = PortfolioLedger(initial_equity=Decimal("10000"))
        for trade in (entry, exit_):
            _persist(session, ledger, trade)
        session.commit()


def test_positions_and_realized_pnl_survive_a_restart(
    factory: sessionmaker[Session],
) -> None:
    _write_round_trip(factory)
    restarted = PortfolioLedger(initial_equity=Decimal("10000"))

    with factory() as session:
        summary = restarted.reconstruct_from_db(session, now=NOW + timedelta(hours=2))

    assert summary.trades_replayed == 2
    assert restarted.position(SYMBOL).is_flat
    # (31000 - 30000) * 0.1 - 1.5 - 1.55
    assert restarted.realized_pnl == Decimal("96.95")
    assert restarted.equity == Decimal("10096.95")
    assert summary.daily_realized_pnl == Decimal("96.95")


def test_open_position_is_restored_with_its_cost_basis(
    factory: sessionmaker[Session],
) -> None:
    trade = Trade(
        order_id="ord_reconstruct_open",
        symbol=SYMBOL,
        side=Side.BUY,
        qty=Decimal("0.05"),
        price=Decimal("30000"),
        fee=Decimal("0.75"),
        filled_at=NOW,
        strategy_id="simple_ema_cross",
    )
    with factory() as session:
        ledger = PortfolioLedger(initial_equity=Decimal("10000"))
        _persist(session, ledger, trade)
        session.commit()

    restarted = PortfolioLedger(initial_equity=Decimal("10000"))
    with factory() as session:
        summary = restarted.reconstruct_from_db(session, now=NOW)

    assert summary.open_positions == 1
    position = restarted.position(SYMBOL)
    assert position.qty == Decimal("0.05")
    assert position.avg_entry == Decimal("30000")
    assert restarted.equity == Decimal("9999.25")


def test_api_startup_restores_the_ledger(factory: sessionmaker[Session]) -> None:
    _write_round_trip(factory)
    settings = load_settings("paper")
    service = ApiService(
        settings=settings,
        repository=SqlTradingRepository(factory),
        ledger=PortfolioLedger(initial_equity=settings.risk.initial_equity),
    )

    class Logger:
        def info(self, *args: object, **kwargs: object) -> None:
            pass

        def warning(self, *args: object, **kwargs: object) -> None:
            pass

    _restore_ledger(service, factory, Logger())

    assert service.ledger.realized_pnl == Decimal("96.95")
    budget = service.risk_budget()
    assert Decimal(budget.daily_realized_pnl) == Decimal("96.95")


def test_stored_position_mismatch_is_reported(
    factory: sessionmaker[Session],
) -> None:
    _write_round_trip(factory)
    with factory() as session:
        session.execute(
            text("update trading.positions set qty = 5, avg_entry = 1 where symbol = :symbol"),
            {"symbol": SYMBOL},
        )
        session.commit()

    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    with factory() as session:
        summary = ledger.reconstruct_from_db(session, strict=False)

    assert summary.trades_replayed == 2
    assert ledger.position(SYMBOL).qty == 0
