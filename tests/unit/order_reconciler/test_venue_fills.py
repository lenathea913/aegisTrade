"""A stop the venue executed is a fill, not a drift.

The reconciler used to halt entries whenever the book disagreed with the exchange, which
made a working stop look like a broken ledger. These tests pin the new order of business:
ask the venue what it executed, book it, and only report what is left unexplained.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from aegis_core import PositionSide, Side, Trade
from aegis_ledger import PortfolioLedger
from order_reconciler.positions import PROTECTION_STRATEGY_ID, PositionReconciler, VenueFill

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def leg(side: PositionSide, qty: str, entry: str = "30000", *, leverage: int = 3) -> Any:
    return SimpleNamespace(
        symbol="BTC/USDT",
        position_side=side,
        qty=Decimal(qty),
        entry_price=Decimal(entry),
        leverage=leverage,
    )


def ledger_with(qty: str, price: str = "30000", *, leverage: int = 3) -> PortfolioLedger:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(
        Trade(
            order_id="ord_1",
            symbol="BTC/USDT",
            side=Side.BUY if Decimal(qty) > 0 else Side.SELL,
            qty=abs(Decimal(qty)),
            price=Decimal(price),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
            leverage=leverage,
        )
    )
    return ledger


def fill(qty: str, price: str, *, order_id: str = "555", side: Side = Side.SELL) -> VenueFill:
    return VenueFill(
        order_id=order_id,
        symbol="BTC/USDT",
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        filled_at=NOW,
    )


class FakeVenue:
    def __init__(self, legs: tuple[Any, ...]) -> None:
        self._legs = legs

    def fetch_positions(self) -> tuple[Any, ...]:
        return self._legs


class FakeFills:
    def __init__(self, fills: tuple[VenueFill, ...] = ()) -> None:
        self.fills = fills
        self.requests: list[tuple[str, datetime | None]] = []

    def fetch_protection_fills(
        self, symbol: str, *, since: datetime | None = None
    ) -> tuple[VenueFill, ...]:
        self.requests.append((symbol, since))
        return self.fills


class FakeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Trade, ...], Any]] = []

    def record_venue_fills(self, *, symbol: str, trades: Any, position: Any) -> None:
        self.calls.append((symbol, tuple(trades), position))


class FakeState:
    def __init__(self) -> None:
        self.synced = 0
        self.blocked: list[Any] = []

    def record_synced(self, at: datetime) -> None:
        self.synced += 1

    def block_entries(self, drifts: Any, at: datetime) -> None:
        self.blocked.append(list(drifts))


def build(
    ledger: PortfolioLedger,
    venue: FakeVenue,
    *,
    fills: FakeFills | None = None,
    recorder: FakeRecorder | None = None,
) -> tuple[PositionReconciler, FakeState]:
    state = FakeState()
    reconciler = PositionReconciler(
        ledger=ledger,
        venue=venue,
        state=state,
        clock=lambda: NOW,
        fills=fills,
        recorder=recorder,
    )
    return reconciler, state


def test_a_stop_fill_that_flattened_the_venue_is_booked_instead_of_halting() -> None:
    ledger = ledger_with("0.1")
    recorder = FakeRecorder()
    reconciler, state = build(
        ledger,
        FakeVenue(()),  # the venue no longer holds anything: the stop fired
        fills=FakeFills((fill("0.1", "29400"),)),
        recorder=recorder,
    )

    assert reconciler.run() == ()
    assert state.synced == 1 and state.blocked == []
    assert len(recorder.calls) == 1
    symbol, trades, position = recorder.calls[0]
    assert symbol == "BTC/USDT"
    assert [trade.strategy_id for trade in trades] == [PROTECTION_STRATEGY_ID]
    assert trades[0].side is Side.SELL and trades[0].qty == Decimal("0.1")
    assert position.is_flat
    # The in-memory book must agree too, or the next pass would see the same gap.
    assert ledger.position("BTC/USDT").is_flat


def test_a_partial_exit_is_booked_when_it_lands_on_the_venue_quantity() -> None:
    ledger = ledger_with("0.1")
    recorder = FakeRecorder()
    reconciler, state = build(
        ledger,
        FakeVenue((leg(PositionSide.LONG, "0.04"),)),
        fills=FakeFills((fill("0.06", "29500"),)),
        recorder=recorder,
    )

    assert reconciler.run() == ()
    assert state.synced == 1
    assert ledger.position("BTC/USDT").qty == Decimal("0.04")
    assert recorder.calls[0][2].qty == Decimal("0.04")


def test_a_fill_that_does_not_explain_the_gap_stays_a_drift() -> None:
    recorder = FakeRecorder()
    reconciler, state = build(
        ledger_with("0.1"),
        FakeVenue(()),
        # Only part of the exposure moved: the rest is still unexplained.
        fills=FakeFills((fill("0.04", "29400"),)),
        recorder=recorder,
    )

    drifts = reconciler.run()

    assert [drift.kind for drift in drifts] == ["missing_at_venue"]
    assert recorder.calls == []
    assert len(state.blocked) == 1


def test_a_fill_larger_than_the_position_is_refused() -> None:
    recorder = FakeRecorder()
    reconciler, _state = build(
        ledger_with("0.1"),
        FakeVenue(()),
        fills=FakeFills((fill("0.3", "29400"),)),
        recorder=recorder,
    )

    assert [drift.kind for drift in reconciler.run()] == ["missing_at_venue"]
    assert recorder.calls == []


def test_an_entry_fill_is_not_an_exit() -> None:
    recorder = FakeRecorder()
    reconciler, _state = build(
        ledger_with("0.1"),
        FakeVenue(()),
        fills=FakeFills((fill("0.1", "29400", side=Side.BUY),)),
        recorder=recorder,
    )

    assert [drift.kind for drift in reconciler.run()] == ["missing_at_venue"]
    assert recorder.calls == []


def test_without_fill_history_every_gap_is_still_a_drift() -> None:
    """The safe default: no venue history configured means no silent forgiveness."""
    reconciler, state = build(ledger_with("0.1"), FakeVenue(()))

    assert [drift.kind for drift in reconciler.run()] == ["missing_at_venue"]
    assert len(state.blocked) == 1


def test_the_fill_lookup_starts_at_the_position_opening() -> None:
    fills = FakeFills((fill("0.1", "29400"),))
    ledger = ledger_with("0.1")
    reconciler, _state = build(ledger, FakeVenue(()), fills=fills, recorder=FakeRecorder())

    reconciler.run()

    # Only exits after this leg opened can explain this leg's gap.
    assert fills.requests == [("BTC/USDT", ledger.position("BTC/USDT").opened_at)]
