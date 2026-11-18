"""Re-arming the venue stop after a restart.

A restored position row proves a stop *should* exist, never that it does. These tests
cover the three answers the venue can give: the stop is still working, it is gone (so it
is re-placed), or the venue could not be asked (so nothing is touched).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from aegis_alerting import Alert, AlertLevel
from aegis_core import Side, Trade
from aegis_execution import ExchangeUnavailableError
from aegis_ledger import AuditEvent, EventType, PortfolioLedger, PositionProtection
from order_reconciler.protection import ProtectionReArmer

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
STOP_ID = "111"


def ledger_with(
    *,
    stop_loss: str | None = "29400",
    take_profit: str | None = "31200",
    stop_id: str | None = STOP_ID,
) -> PortfolioLedger:
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
            leverage=3,
        )
    )
    if stop_loss is not None:
        ledger.set_protection(
            PositionProtection(
                symbol="BTC/USDT",
                side=Side.BUY,
                qty=Decimal("0.1"),
                stop_loss=Decimal(stop_loss),
                take_profit=None if take_profit is None else Decimal(take_profit),
                stop_order_id=stop_id,
                tp_order_id="222" if take_profit is not None else None,
            )
        )
    return ledger


class FakeVenue:
    def __init__(self, open_ids: set[str] | None = None, error: Exception | None = None) -> None:
        self.open_ids = open_ids or set()
        self.error = error
        self.cancelled: list[str] = []
        self.placed: list[dict[str, Any]] = []

    def fetch_open_order_ids(self, symbol: str) -> set[str]:
        if self.error is not None:
            raise self.error
        return set(self.open_ids)

    def cancel_protection_orders(self, symbol: str) -> int:
        self.cancelled.append(symbol)
        return 1

    def submit_protection_order(self, **kwargs: Any) -> SimpleNamespace:
        self.placed.append(kwargs)
        return SimpleNamespace(stop_order_id="9001", take_order_id="9002")


class FakeWriter:
    def __init__(self) -> None:
        self.positions: list[Any] = []
        self.audits: list[AuditEvent] = []

    def save_order(self, order: Any, approved: Any) -> None:
        return None

    def save_trade(self, trade: Any) -> None:
        return None

    def save_position(self, position: Any) -> None:
        self.positions.append(position)

    def append_audit(self, event: AuditEvent) -> None:
        self.audits.append(event)


class FakeAlerter:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        return True


def build(
    ledger: PortfolioLedger, venue: FakeVenue
) -> tuple[ProtectionReArmer, FakeWriter, FakeAlerter]:
    writer, alerter = FakeWriter(), FakeAlerter()

    @contextmanager
    def factory() -> Iterator[FakeWriter]:
        yield writer

    rearmer = ProtectionReArmer(
        ledger=ledger,
        venue=venue,
        writer_factory=factory,
        alerter=alerter,
        clock=lambda: NOW,
    )
    return rearmer, writer, alerter


def test_a_stop_that_is_still_working_is_left_alone() -> None:
    venue = FakeVenue({STOP_ID, "222"})
    rearmer, writer, alerter = build(ledger_with(), venue)

    results = rearmer.run()

    assert [result.status for result in results] == ["intact"]
    assert venue.placed == [] and venue.cancelled == []
    assert writer.positions == [] and alerter.alerts == []


def test_a_stop_cancelled_by_hand_is_re_placed_and_remembered() -> None:
    ledger = ledger_with()
    venue = FakeVenue(set())
    rearmer, writer, alerter = build(ledger, venue)

    results = rearmer.run()

    assert [result.status for result in results] == ["rearmed"]
    # Stale exits go first so the leg can never hold two stops at once.
    assert venue.cancelled == ["BTC/USDT"]
    assert len(venue.placed) == 1
    placed = venue.placed[0]
    assert placed["side"] is Side.SELL
    assert placed["stop_loss"] == Decimal("29400")
    assert placed["take_profit"] == Decimal("31200")
    # The new id reaches the position row that a restart would read.
    assert writer.positions[0].stop_order_id == "9001"
    assert ledger.position("BTC/USDT").stop_order_id == "9001"
    assert [event.event_type for event in writer.audits] == [EventType.PROTECTION_REARMED]
    assert writer.audits[0].payload["previous_stop_order_id"] == STOP_ID
    assert alerter.alerts[0].level is AlertLevel.WARNING


def test_a_position_without_a_recorded_stop_is_also_re_armed() -> None:
    venue = FakeVenue(set())
    rearmer, writer, _alerter = build(ledger_with(stop_id=None), venue)

    results = rearmer.run()

    assert [result.status for result in results] == ["rearmed"]
    assert writer.positions[0].stop_order_id == "9001"


def test_a_position_without_a_level_is_reported_never_guessed() -> None:
    venue = FakeVenue(set())
    rearmer, writer, alerter = build(ledger_with(stop_loss=None), venue)

    results = rearmer.run()

    assert [result.status for result in results] == ["no_stop_level"]
    assert venue.placed == [] and writer.positions == []
    assert alerter.alerts[0].level is AlertLevel.CRITICAL
    assert alerter.alerts[0].context["status"] == "UNPROTECTED"


def test_an_unreachable_venue_leaves_the_stop_untouched() -> None:
    venue = FakeVenue(error=ExchangeUnavailableError("timeout"))
    rearmer, writer, _alerter = build(ledger_with(), venue)

    results = rearmer.run()

    # Unknown is not "naked": cancelling and re-placing blind could duplicate the stop.
    assert [result.status for result in results] == ["unchecked"]
    assert venue.cancelled == [] and venue.placed == []
    assert writer.positions == []


def test_a_flat_book_needs_no_check() -> None:
    venue = FakeVenue(set())
    rearmer, _writer, _alerter = build(PortfolioLedger(initial_equity=Decimal("10000")), venue)

    assert rearmer.run() == ()
