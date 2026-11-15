"""Ledger reconstruction from the durable record (no database needed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from aegis_ledger import LedgerError, PortfolioLedger

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def trade_row(
    *,
    trade_id: str,
    side: str,
    qty: str,
    price: str,
    fee: str = "0",
    funding: str = "0",
    at: datetime = NOW,
    symbol: str = "BTC/USDT",
) -> SimpleNamespace:
    return SimpleNamespace(
        trade_id=trade_id,
        order_id="ord_1",
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        funding=Decimal(funding),
        filled_at=at,
        strategy_id="simple_ema_cross",
        r_multiple=None,
    )


def position_row(
    *,
    qty: str,
    avg_entry: str,
    realized: str = "0",
    symbol: str = "BTC/USDT",
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        qty=Decimal(qty),
        avg_entry=Decimal(avg_entry),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal(realized),
        opened_at=NOW,
    )


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> list[Any]:
        return self._rows


class FakeSession:
    """Routes each ``execute`` to the right table by inspecting the statement."""

    def __init__(self, trades: list[Any], positions: list[Any]) -> None:
        self._trades = trades
        self._positions = positions
        self.queries = 0

    def execute(self, statement: Any) -> _Result:
        self.queries += 1
        sql = str(statement)
        return _Result(self._positions if "trading.positions" in sql else self._trades)


def test_replay_restores_position_and_windows() -> None:
    trades = [
        trade_row(trade_id="t1", side="BUY", qty="0.1", price="30000", fee="1.5"),
        trade_row(
            trade_id="t2",
            side="SELL",
            qty="0.1",
            price="31000",
            fee="1.55",
            at=NOW + timedelta(hours=1),
        ),
    ]
    # Position.realized_pnl is gross (fees live in the ledger's realized events).
    session = FakeSession(trades, [position_row(qty="0", avg_entry="0", realized="100")])
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    summary = ledger.reconstruct_from_db(session, now=NOW + timedelta(hours=2))

    assert summary.trades_replayed == 2
    assert summary.positions_loaded == 1
    assert summary.open_positions == 0
    # (31000 - 30000) * 0.1 - 1.5 - 1.55 = 96.95 realized
    assert summary.daily_realized_pnl == Decimal("96.95")
    assert summary.weekly_realized_pnl == Decimal("96.95")
    assert summary.equity == Decimal("10096.95")
    assert ledger.position("BTC/USDT").is_flat
    assert len(ledger.realized_events) == 2


def test_open_position_is_rebuilt_and_marked_at_last_fill() -> None:
    trades = [trade_row(trade_id="t1", side="BUY", qty="0.1", price="30000", fee="1.5")]
    session = FakeSession(trades, [position_row(qty="0.1", avg_entry="30000", realized="0")])
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    summary = ledger.reconstruct_from_db(session, now=NOW)

    position = ledger.position("BTC/USDT")
    assert position.qty == Decimal("0.1")
    assert position.avg_entry == Decimal("30000")
    assert ledger.mark("BTC/USDT") == Decimal("30000")
    assert summary.open_positions == 1
    assert summary.equity == Decimal("9998.5")  # fee already paid


def test_disagreement_with_positions_is_refused_by_default() -> None:
    trades = [trade_row(trade_id="t1", side="BUY", qty="0.1", price="30000")]
    session = FakeSession(trades, [position_row(qty="9", avg_entry="30000")])
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    with pytest.raises(LedgerError, match="disagrees"):
        ledger.reconstruct_from_db(session)


def test_disagreement_can_be_inspected_without_raising() -> None:
    trades = [trade_row(trade_id="t1", side="BUY", qty="0.1", price="30000")]
    session = FakeSession(trades, [position_row(qty="0.2", avg_entry="30000")])
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    summary = ledger.reconstruct_from_db(session, strict=False)

    assert summary.trades_replayed == 1
    assert ledger.position("BTC/USDT").qty == Decimal("0.1")


def test_an_empty_database_restores_a_clean_book() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    summary = ledger.reconstruct_from_db(FakeSession([], []), now=NOW)

    assert summary.trades_replayed == 0
    assert summary.equity == Decimal("10000")
    assert ledger.positions == ()
