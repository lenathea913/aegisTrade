"""Position reconciliation: the book versus the venue.

A drift is not a rounding detail — it is the ledger risking money it may not have — so
these tests check both halves: the comparison, and the halt that follows a mismatch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from aegis_alerting import Alert, AlertLevel
from aegis_core import PositionSide, Side, Trade
from aegis_execution import ExchangeUnavailableError
from aegis_ledger import PortfolioLedger
from order_reconciler.positions import PositionDrift, PositionReconciler
from order_reconciler.state import ReconciliationSnapshot

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def leg(side: PositionSide, qty: str, entry: str, *, leverage: int = 3) -> Any:
    return SimpleNamespace(
        symbol="BTC/USDT",
        position_side=side,
        qty=Decimal(qty),
        entry_price=Decimal(entry),
        leverage=leverage,
    )


def ledger_with(side: Side, qty: str, price: str, *, leverage: int = 3) -> PortfolioLedger:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(
        Trade(
            order_id="ord_1",
            symbol="BTC/USDT",
            side=side,
            qty=Decimal(qty),
            price=Decimal(price),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
            leverage=leverage,
        )
    )
    return ledger


class FakeVenue:
    def __init__(self, legs: tuple[Any, ...] = (), error: Exception | None = None) -> None:
        self._legs = legs
        self._error = error

    def fetch_positions(self) -> tuple[Any, ...]:
        if self._error is not None:
            raise self._error
        return self._legs


class FakeState:
    def __init__(self) -> None:
        self.synced = 0
        self.blocked: list[tuple[PositionDrift, ...]] = []

    def record_synced(self, at: datetime) -> None:
        self.synced += 1

    def block_entries(self, drifts: Any, at: datetime) -> None:
        self.blocked.append(tuple(drifts))


class FakeAlerter:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        return True


def build(
    ledger: PortfolioLedger, venue: FakeVenue, state: FakeState, alerter: FakeAlerter
) -> PositionReconciler:
    return PositionReconciler(
        ledger=ledger,
        venue=venue,
        state=state,
        alerter=alerter,
        clock=lambda: NOW,
    )


def test_a_matching_book_is_recorded_as_synced() -> None:
    state, alerter = FakeState(), FakeAlerter()
    reconciler = build(
        ledger_with(Side.BUY, "0.1", "30000"),
        FakeVenue((leg(PositionSide.LONG, "0.1", "30000"),)),
        state,
        alerter,
    )

    assert reconciler.run() == ()
    assert state.synced == 1
    assert state.blocked == []
    assert alerter.alerts == []


def test_a_quantity_mismatch_halts_entries_and_alerts() -> None:
    state, alerter = FakeState(), FakeAlerter()
    reconciler = build(
        ledger_with(Side.BUY, "0.1", "30000"),
        FakeVenue((leg(PositionSide.LONG, "0.05", "30000"),)),
        state,
        alerter,
    )

    drifts = reconciler.run()

    assert [drift.kind for drift in drifts] == ["qty_mismatch"]
    assert drifts[0].ledger_qty == Decimal("0.1")
    assert drifts[0].venue_qty == Decimal("0.05")
    assert len(state.blocked) == 1
    assert state.synced == 0
    assert alerter.alerts[0].level is AlertLevel.CRITICAL
    assert "Ledger drift" in alerter.alerts[0].title


def test_a_position_only_on_the_venue_is_reported() -> None:
    state = FakeState()
    reconciler = build(
        PortfolioLedger(initial_equity=Decimal("10000")),
        FakeVenue((leg(PositionSide.SHORT, "0.2", "31000"),)),
        state,
        FakeAlerter(),
    )

    drifts = reconciler.run()

    assert [drift.kind for drift in drifts] == ["missing_in_ledger"]
    assert drifts[0].venue_qty == Decimal("-0.2")
    assert len(state.blocked) == 1


def test_a_position_only_in_the_ledger_is_reported() -> None:
    state = FakeState()
    reconciler = build(
        ledger_with(Side.BUY, "0.1", "30000"),
        FakeVenue(()),
        state,
        FakeAlerter(),
    )

    drifts = reconciler.run()

    assert [drift.kind for drift in drifts] == ["missing_at_venue"]


def test_an_entry_within_tolerance_is_not_a_drift() -> None:
    state = FakeState()
    reconciler = build(
        ledger_with(Side.BUY, "0.1", "30000"),
        # 0.1 % above the book: the venue reports a slightly different average.
        FakeVenue((leg(PositionSide.LONG, "0.1", "30030"),)),
        state,
        FakeAlerter(),
    )

    assert reconciler.run() == ()
    assert state.synced == 1


def test_a_different_entry_price_is_a_drift() -> None:
    state = FakeState()
    reconciler = build(
        ledger_with(Side.BUY, "0.1", "30000"),
        FakeVenue((leg(PositionSide.LONG, "0.1", "33000"),)),
        state,
        FakeAlerter(),
    )

    assert [drift.kind for drift in reconciler.run()] == ["entry_mismatch"]


def test_a_leverage_mismatch_is_a_drift() -> None:
    state = FakeState()
    reconciler = build(
        ledger_with(Side.BUY, "0.1", "30000", leverage=3),
        FakeVenue((leg(PositionSide.LONG, "0.1", "30000", leverage=5),)),
        state,
        FakeAlerter(),
    )

    assert [drift.kind for drift in reconciler.run()] == ["leverage_mismatch"]


def test_hedged_legs_are_summed_into_one_signed_exposure() -> None:
    state = FakeState()
    reconciler = build(
        PortfolioLedger(initial_equity=Decimal("10000")),
        FakeVenue(
            (
                leg(PositionSide.LONG, "0.1", "30000"),
                leg(PositionSide.SHORT, "0.1", "31000"),
            )
        ),
        state,
        FakeAlerter(),
    )

    assert reconciler.run() == ()
    assert state.synced == 1


def test_an_unreachable_venue_is_not_treated_as_agreement() -> None:
    state, alerter = FakeState(), FakeAlerter()
    reconciler = build(
        ledger_with(Side.BUY, "0.1", "30000"),
        FakeVenue(error=ExchangeUnavailableError("timeout")),
        state,
        alerter,
    )

    assert reconciler.run() == ()
    assert state.synced == 0
    assert state.blocked == []
    assert alerter.alerts == []


def test_the_snapshot_explains_why_entries_are_off() -> None:
    halted = ReconciliationSnapshot(
        status="drift",
        entries_halted=True,
        drift_count=2,
        detail=(),
        checked_at=NOW,
        detected_at=NOW,
    )
    assert halted.blocked_reason == "ledger_drift:2"
    assert halted.label == "Ledger drift!"

    synced = ReconciliationSnapshot(
        status="synced",
        entries_halted=False,
        drift_count=0,
        detail=(),
        checked_at=NOW,
        detected_at=None,
    )
    assert synced.blocked_reason is None
    assert synced.label == "Ledger synced"

    unchecked = ReconciliationSnapshot(
        status="unchecked",
        entries_halted=False,
        drift_count=0,
        detail=(),
        checked_at=None,
        detected_at=None,
    )
    assert unchecked.label == "Ledger unchecked"


@pytest.mark.parametrize("kind", ["qty_mismatch", "missing_in_ledger"])
def test_every_drift_payload_is_json_ready(kind: str) -> None:
    drift = PositionDrift(
        symbol="BTC/USDT",
        kind=kind,
        ledger_qty=Decimal("0.1"),
        venue_qty=Decimal("-0.2"),
        ledger_entry=Decimal("30000"),
        venue_entry=Decimal("31000"),
        ledger_leverage=3,
        venue_leverage=5,
    )

    payload = drift.as_payload()

    assert all(isinstance(value, str) or value is None for value in payload.values())
    assert "BTC/USDT" in drift.detail
