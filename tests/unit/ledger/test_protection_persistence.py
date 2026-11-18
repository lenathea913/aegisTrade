"""Protective levels must survive a restart.

The failure these tests exist for: ``reconstruct_from_db`` used to rebuild positions and
then clear ``_protections``, so a restarted process ran with open positions and nothing
watching them. The stop now travels with the position row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from aegis_core import Side, Trade
from aegis_ledger import PortfolioLedger, PositionProtection
from aegis_ledger.records import position_from_row, position_row

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _trade_row(*, trade_id: str, side: str, qty: str, price: str, at: datetime = NOW) -> Any:
    return SimpleNamespace(
        trade_id=trade_id,
        order_id="ord_1",
        symbol="BTC/USDT",
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        funding=Decimal("0"),
        filled_at=at,
        strategy_id="simple_ema_cross",
        r_multiple=None,
        leverage=1,
    )


def _position_row(
    *,
    qty: str,
    avg_entry: str,
    stop_loss: str | None,
    take_profit: str | None = None,
    stop_order_id: str | None = None,
    tp_order_id: str | None = None,
) -> Any:
    return SimpleNamespace(
        symbol="BTC/USDT",
        qty=Decimal(qty),
        avg_entry=Decimal(avg_entry),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        opened_at=NOW,
        leverage=1,
        stop_loss=None if stop_loss is None else Decimal(stop_loss),
        take_profit=None if take_profit is None else Decimal(take_profit),
        stop_order_id=stop_order_id,
        tp_order_id=tp_order_id,
    )


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> list[Any]:
        return self._rows


class FakeSession:
    """Routes ``execute`` to the right table by inspecting the statement."""

    def __init__(self, trades: list[Any], positions: list[Any]) -> None:
        self._trades = trades
        self._positions = positions

    def execute(self, statement: Any) -> _Result:
        sql = str(statement)
        return _Result(self._positions if "trading.positions" in sql else self._trades)


def test_a_reconstructed_position_gets_its_stop_back() -> None:
    session = FakeSession(
        trades=[_trade_row(trade_id="t1", side="BUY", qty="0.1", price="30000")],
        positions=[_position_row(qty="0.1", avg_entry="30000", stop_loss="29400")],
    )
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    ledger.reconstruct_from_db(session, now=NOW)

    protection = ledger.protection("BTC/USDT")
    assert protection is not None
    assert protection.side is Side.BUY
    assert protection.qty == Decimal("0.1")
    assert protection.stop_loss == Decimal("29400")
    # The level also travels on the position, which is what gets persisted next time.
    assert ledger.position("BTC/USDT").stop_loss == Decimal("29400")


def test_a_stored_target_is_restored_beside_the_stop() -> None:
    session = FakeSession(
        trades=[_trade_row(trade_id="t1", side="SELL", qty="0.1", price="30000")],
        positions=[
            _position_row(qty="-0.1", avg_entry="30000", stop_loss="30700", take_profit="28600")
        ],
    )
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    ledger.reconstruct_from_db(session, now=NOW)

    protection = ledger.protection("BTC/USDT")
    assert protection is not None
    assert protection.side is Side.SELL
    assert protection.take_profit == Decimal("28600")


def test_a_row_without_levels_stays_unprotected_instead_of_guessing() -> None:
    session = FakeSession(
        trades=[_trade_row(trade_id="t1", side="BUY", qty="0.1", price="30000")],
        positions=[_position_row(qty="0.1", avg_entry="30000", stop_loss=None)],
    )
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    ledger.reconstruct_from_db(session, now=NOW)

    assert ledger.protection("BTC/USDT") is None
    assert ledger.position("BTC/USDT").stop_loss is None


def test_setting_a_protection_writes_it_onto_the_position() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(
        Trade(
            order_id="ord_1",
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.1"),
            price=Decimal("30000"),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
        )
    )

    position = ledger.set_protection(
        PositionProtection(
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.1"),
            stop_loss=Decimal("29400"),
            take_profit=Decimal("31200"),
        )
    )

    assert position.stop_loss == Decimal("29400")
    assert position.take_profit == Decimal("31200")
    assert ledger.position("BTC/USDT").stop_loss == Decimal("29400")


def test_clearing_a_protection_removes_the_levels_from_the_position() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(
        Trade(
            order_id="ord_1",
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.1"),
            price=Decimal("30000"),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
        )
    )
    ledger.set_protection(
        PositionProtection(
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.1"),
            stop_loss=Decimal("29400"),
        )
    )

    ledger.clear_protection("BTC/USDT")

    assert ledger.protection("BTC/USDT") is None
    assert ledger.position("BTC/USDT").stop_loss is None


def test_closing_the_position_drops_the_levels() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(
        Trade(
            order_id="ord_1",
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.1"),
            price=Decimal("30000"),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
        )
    )
    ledger.set_protection(
        PositionProtection(
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.1"),
            stop_loss=Decimal("29400"),
        )
    )

    update = ledger.apply_trade(
        Trade(
            order_id="ord_2",
            symbol="BTC/USDT",
            side=Side.SELL,
            qty=Decimal("0.1"),
            price=Decimal("30200"),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
        )
    )

    assert update.position.is_flat
    assert update.position.stop_loss is None
    assert ledger.protection("BTC/USDT") is None


def test_a_flat_position_may_not_carry_levels() -> None:
    with pytest.raises(ValueError, match="flat position cannot carry"):
        position_from_row(_position_row(qty="0", avg_entry="0", stop_loss="29400"))


def test_the_row_carries_the_levels_for_the_database() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(
        Trade(
            order_id="ord_1",
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.1"),
            price=Decimal("30000"),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
        )
    )
    position = ledger.set_protection(
        PositionProtection(
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.1"),
            stop_loss=Decimal("29400"),
        )
    )

    row = position_row(position)

    assert row["stop_loss"] == Decimal("29400")
    assert row["take_profit"] is None


def test_the_venue_order_ids_survive_a_restart() -> None:
    session = FakeSession(
        trades=[_trade_row(trade_id="t1", side="BUY", qty="0.1", price="30000")],
        positions=[
            _position_row(
                qty="0.1",
                avg_entry="30000",
                stop_loss="29400",
                take_profit="31200",
                stop_order_id="9001",
                tp_order_id="9002",
            )
        ],
    )
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    ledger.reconstruct_from_db(session, now=NOW)

    protection = ledger.protection("BTC/USDT")
    assert protection is not None
    assert protection.stop_order_id == "9001"
    assert protection.tp_order_id == "9002"
    # The ids travel on the position, which is the row the re-armer checks.
    assert ledger.position("BTC/USDT").stop_order_id == "9001"


def test_the_row_carries_the_venue_order_ids() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(
        Trade(
            order_id="ord_1",
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.1"),
            price=Decimal("30000"),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
        )
    )
    position = ledger.set_protection(
        PositionProtection(
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.1"),
            stop_loss=Decimal("29400"),
            stop_order_id="9001",
        )
    )

    row = position_row(position)

    assert row["stop_order_id"] == "9001"
    assert row["tp_order_id"] is None
