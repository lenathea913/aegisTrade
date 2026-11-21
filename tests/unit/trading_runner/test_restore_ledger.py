"""A failed startup replay must be visible, because the runner blocks entries on it."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from aegis_ledger import PortfolioLedger
from trading_runner.cli import restore_ledger


class BrokenSession:
    """A session whose first query fails, like an unreachable database."""

    def execute(self, statement: Any) -> Any:
        raise SQLAlchemyError("database unavailable")

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


class BrokenFactory:
    def __call__(self) -> BrokenSession:
        return BrokenSession()


def test_a_failed_replay_returns_false_and_leaves_the_book_empty() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    restored = restore_ledger(ledger, cast(sessionmaker[Session], BrokenFactory()))

    # False is the signal build_runner turns into `entries_blocked = ledger_unrestored`.
    # Returning None here (the old behaviour) let a zero loss window trade.
    assert restored is False
    assert ledger.positions == ()
    assert ledger.realized_pnl == Decimal("0")
