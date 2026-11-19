"""What a restarted monitor learns from ``trading.positions``.

The monitor only watches positions that have an entry in ``ledger.protections``, so a
restore that loses a symbol is not a display bug: that position is never checked.
These tests pin the row as the source: levels, leverage and the venue order ids all
come from it, not from a window of recent orders.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from aegis_ledger import PortfolioLedger
from position_monitor.cli import restore_from_database

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def row(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "symbol": "BTC/USDT",
        "qty": Decimal("0.1"),
        "avg_entry": Decimal("30000"),
        "unrealized_pnl": Decimal("0"),
        "realized_pnl": Decimal("0"),
        "opened_at": NOW,
        "leverage": 3,
        "stop_loss": Decimal("29400"),
        "take_profit": Decimal("31200"),
        "stop_order_id": "777",
        "tp_order_id": "888",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeSession:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def execute(self, _statement: Any) -> Any:
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: list(self._rows)),
        )

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def restore(rows: list[Any]) -> tuple[PortfolioLedger, tuple[Any, ...]]:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    factory = cast(Any, lambda: FakeSession(rows))
    positions = restore_from_database(ledger, factory)
    return ledger, positions


def test_the_position_row_supplies_levels_ids_and_leverage() -> None:
    ledger, positions = restore([row()])

    assert [position.symbol for position in positions] == ["BTC/USDT"]
    assert positions[0].leverage == 3
    assert positions[0].stop_order_id == "777"

    protection = ledger.protection("BTC/USDT")
    assert protection is not None
    assert protection.stop_loss == Decimal("29400")
    assert protection.take_profit == Decimal("31200")
    # The ids are what stop the next check from cancelling and re-placing a working stop.
    assert protection.stop_order_id == "777"
    assert protection.tp_order_id == "888"
    assert ledger.position("BTC/USDT").leverage == 3


def test_a_row_without_a_level_is_restored_but_not_watched() -> None:
    ledger, positions = restore(
        [row(stop_loss=None, take_profit=None, stop_order_id=None, tp_order_id=None)]
    )

    assert len(positions) == 1
    assert ledger.protection("BTC/USDT") is None


def test_every_open_symbol_is_restored_not_just_the_busiest() -> None:
    ledger, positions = restore(
        [
            row(),
            row(symbol="ETH/USDT", stop_loss=Decimal("2800"), take_profit=Decimal("3200")),
        ]
    )

    assert {position.symbol for position in positions} == {"BTC/USDT", "ETH/USDT"}
    assert {protection.symbol for protection in ledger.protections} == {
        "BTC/USDT",
        "ETH/USDT",
    }
