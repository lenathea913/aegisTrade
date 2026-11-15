"""Repository mapping and SQL wiring (no database needed)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar, cast

from sqlalchemy.exc import OperationalError

from aegis_ledger import EventType
from api import repository as repository_module
from api.repository import SqlTradingRepository, audit_event_from_row, trade_from_row

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FakeTradeRow:
    trade_id = "trd-1"
    order_id = "ord-1"
    symbol = "BTC/USDT"
    side = "SELL"
    qty = Decimal("0.2")
    price = Decimal("31000")
    fee = Decimal("3.1")
    funding = Decimal("-0.5")
    filled_at = NOW
    strategy_id = "simple_ema_cross"
    r_multiple = Decimal("1.5")


class FakeAuditRow:
    event_type = "trade_recorded"
    entity_type = "trade"
    entity_id = "trd-1"
    actor = "system"
    payload: ClassVar[dict[str, str]] = {"fee": "3.1"}
    created_at = NOW


class FakeSession:
    def __init__(self, rows: Sequence[Any] = (), *, failure: Exception | None = None) -> None:
        self.rows = rows
        self.failure = failure
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> Any:
        if self.failure is not None:
            raise self.failure
        self.statements.append(statement)
        return _Result(self.rows)


class _Result:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = rows

    def scalars(self) -> Iterator[Any]:
        return iter(self._rows)


def test_trade_row_maps_to_the_domain() -> None:
    trade = trade_from_row(cast(Any, FakeTradeRow()))

    assert trade.trade_id == "trd-1"
    assert trade.side.value == "SELL"
    assert trade.fee == Decimal("3.1")
    assert trade.funding == Decimal("-0.5")
    assert trade.r_multiple == Decimal("1.5")


def test_audit_row_maps_to_the_domain() -> None:
    event = audit_event_from_row(cast(Any, FakeAuditRow()))

    assert event.event_type is EventType.TRADE_RECORDED
    assert event.entity_id == "trd-1"
    assert event.payload == {"fee": "3.1"}


def patch_scope(monkeypatch: Any, session: FakeSession) -> None:
    @contextlib.contextmanager
    def fake_scope(factory: object) -> Iterator[FakeSession]:
        yield session

    monkeypatch.setattr(repository_module, "session_scope", fake_scope)


def test_ping_reports_reachability(monkeypatch: Any) -> None:
    patch_scope(monkeypatch, FakeSession())
    assert SqlTradingRepository(cast(Any, object())).ping() is True

    down = OperationalError("SELECT 1", {}, Exception("connection refused"))
    patch_scope(monkeypatch, FakeSession(failure=down))
    assert SqlTradingRepository(cast(Any, object())).ping() is False


def test_trades_and_audit_events_are_read_and_mapped(monkeypatch: Any) -> None:
    patch_scope(monkeypatch, FakeSession([FakeTradeRow()]))
    trades = SqlTradingRepository(cast(Any, object())).trades(10)
    assert [trade.trade_id for trade in trades] == ["trd-1"]

    patch_scope(monkeypatch, FakeSession([FakeAuditRow()]))
    events = SqlTradingRepository(cast(Any, object())).audit_events(10)
    assert [event.entity_id for event in events] == ["trd-1"]
