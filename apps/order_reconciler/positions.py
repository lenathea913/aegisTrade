"""Position reconciliation: does the book hold what the venue holds?

Order reconciliation answers "what happened to this order?". It cannot answer the
question that actually protects the account: *is the position the ledger believes it
has the position that exists on the exchange?* A fill that was never recorded, a leg
closed by hand in the venue UI, a partial close the websocket missed — all of them
leave the ledger convinced of an exposure that is not there, and every risk decision
afterwards is computed against the wrong book.

So this job compares the two sides and, on any disagreement:

1. logs ``position_drift_detected`` at CRITICAL,
2. sends a CRITICAL alert (Telegram when configured),
3. writes ``trading.reconciliation_state`` with ``entries_halted = true``.

The last point is why the flag is in the database rather than in memory: drift usually
ends in a restart, and "no new entries until a human looks" has to survive it. The
protective monitor keeps running, because an open position still needs its stop.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from aegis_alerting import Alert, Alerter, AlertLevel
from aegis_core import utc_now
from aegis_execution import ExchangeError
from aegis_ledger import PortfolioLedger
from aegis_observability import get_logger

__all__ = [
    "DriftStateWriter",
    "PositionDrift",
    "PositionReconciler",
    "VenueLeg",
    "VenuePositionFeed",
]

_QUANTUM = Decimal("0.000000000001")
_ZERO = Decimal("0")
#: Entry prices may differ by fees/rounding between fills and ``entryPrice``; quantity
#: may not. Half a percent is wide enough for a weighted average, narrow enough that a
#: different position is never mistaken for the same one.
ENTRY_TOLERANCE_PCT = Decimal("0.5")
_HUNDRED = Decimal("100")


class VenueLeg(Protocol):
    """One leg as the venue reports it (``FuturesPosition`` satisfies this)."""

    symbol: str
    position_side: object
    qty: Decimal
    entry_price: Decimal
    leverage: int


@dataclass(frozen=True)
class PositionDrift:
    """One disagreement between the ledger and the venue."""

    symbol: str
    kind: str
    ledger_qty: Decimal
    venue_qty: Decimal
    ledger_entry: Decimal | None = None
    venue_entry: Decimal | None = None
    ledger_leverage: int | None = None
    venue_leverage: int | None = None

    @property
    def detail(self) -> str:
        return (
            f"{self.symbol}: {self.kind} "
            f"(ledger qty={self.ledger_qty} entry={self.ledger_entry} "
            f"leverage={self.ledger_leverage} vs venue qty={self.venue_qty} "
            f"entry={self.venue_entry} leverage={self.venue_leverage})"
        )

    def as_payload(self) -> dict[str, str | None]:
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "ledger_qty": str(self.ledger_qty),
            "venue_qty": str(self.venue_qty),
            "ledger_entry": None if self.ledger_entry is None else str(self.ledger_entry),
            "venue_entry": None if self.venue_entry is None else str(self.venue_entry),
            "ledger_leverage": None if self.ledger_leverage is None else str(self.ledger_leverage),
            "venue_leverage": None if self.venue_leverage is None else str(self.venue_leverage),
        }


class VenuePositionFeed(Protocol):
    """The one venue call this job makes."""

    def fetch_positions(self) -> Sequence[VenueLeg]:
        """Every open leg, as the exchange sees it."""


class DriftStateWriter(Protocol):
    """Where the outcome is recorded so the runner can read it before entries."""

    def record_synced(self, at: datetime) -> None:
        """The book and the venue agree."""

    def block_entries(self, drifts: Sequence[PositionDrift], at: datetime) -> None:
        """Stop new entries until an operator acknowledges the drift."""


@dataclass(frozen=True)
class _VenueBook:
    """Net sign exposure per symbol (hedge legs are summed, not netted per leg)."""

    qty: Decimal
    entry: Decimal | None
    leverage: int | None
    legs: int


class PositionReconciler:
    """Compares ``PortfolioLedger`` with the venue, one pass at a time."""

    def __init__(
        self,
        *,
        ledger: PortfolioLedger,
        venue: VenuePositionFeed,
        state: DriftStateWriter,
        alerter: Alerter | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._ledger = ledger
        self._venue = venue
        self._state = state
        self._alerter = alerter
        self._clock = clock
        self._logger = get_logger("aegis.position_reconciler")

    def run(self) -> tuple[PositionDrift, ...]:
        """One pass. An unreachable venue leaves the previous flag untouched."""
        try:
            legs = self._venue.fetch_positions()
        except ExchangeError as exc:
            # Silence is not agreement: do not mark the book synced on a failed read.
            self._logger.warning("position_reconcile_fetch_failed", error=str(exc))
            return ()

        drifts = self.compare(legs)
        now = self._clock()
        if not drifts:
            self._state.record_synced(now)
            self._logger.info("position_reconcile_synced", venue_legs=len(legs))
            return ()

        detail = "; ".join(drift.detail for drift in drifts)
        self._logger.critical(
            "position_drift_detected",
            count=len(drifts),
            detail=detail,
            entries_halted=True,
        )
        if self._alerter is not None:
            self._alerter.send(
                Alert(
                    level=AlertLevel.CRITICAL,
                    title="Ledger drift: entries halted",
                    # The alert body is short by contract; the evidence is in context.
                    message=(
                        f"The book disagrees with the venue on {len(drifts)} symbol(s); "
                        "no new entries until an operator acknowledges it."
                    ),
                    source="position_reconciler",
                    context={
                        "status": "DRIFT",
                        "reason": "position_drift",
                        "symbols": ",".join(drift.symbol for drift in drifts),
                        "detail": detail[:500],
                    },
                )
            )
        self._state.block_entries(drifts, now)
        return tuple(drifts)

    def compare(self, legs: Sequence[VenueLeg]) -> list[PositionDrift]:
        """Pure comparison, exposed so tests can drive it without a venue."""
        venue = _aggregate(legs)
        drifts: list[PositionDrift] = []
        ledger_symbols = {
            position.symbol for position in self._ledger.positions if not position.is_flat
        }
        for symbol in sorted(ledger_symbols | set(venue)):
            position = self._ledger.position(symbol)
            leg = venue.get(symbol)
            ledger_qty = position.qty if not position.is_flat else _ZERO
            venue_qty = _ZERO if leg is None else leg.qty
            if leg is not None and _normalise(venue_qty) == 0 and position.is_flat:
                # A hedged pair that nets to zero is flat on both sides, not a drift.
                continue

            if leg is None:
                drifts.append(
                    PositionDrift(
                        symbol=symbol,
                        kind="missing_at_venue",
                        ledger_qty=ledger_qty,
                        venue_qty=_ZERO,
                        ledger_entry=None if position.is_flat else position.avg_entry,
                        ledger_leverage=None if position.is_flat else position.leverage,
                    )
                )
                continue
            if position.is_flat:
                drifts.append(
                    PositionDrift(
                        symbol=symbol,
                        kind="missing_in_ledger",
                        ledger_qty=_ZERO,
                        venue_qty=venue_qty,
                        venue_entry=leg.entry,
                        venue_leverage=leg.leverage,
                    )
                )
                continue
            if _normalise(ledger_qty) != _normalise(venue_qty):
                drifts.append(
                    PositionDrift(
                        symbol=symbol,
                        kind="qty_mismatch",
                        ledger_qty=ledger_qty,
                        venue_qty=venue_qty,
                        ledger_entry=position.avg_entry,
                        ledger_leverage=position.leverage,
                        venue_entry=leg.entry,
                        venue_leverage=leg.leverage,
                    )
                )
                continue
            if leg.entry is not None and _entry_differs(position.avg_entry, leg.entry):
                drifts.append(
                    PositionDrift(
                        symbol=symbol,
                        kind="entry_mismatch",
                        ledger_qty=ledger_qty,
                        venue_qty=venue_qty,
                        ledger_entry=position.avg_entry,
                        venue_entry=leg.entry,
                        ledger_leverage=position.leverage,
                        venue_leverage=leg.leverage,
                    )
                )
                continue
            if leg.leverage is not None and position.leverage != leg.leverage:
                drifts.append(
                    PositionDrift(
                        symbol=symbol,
                        kind="leverage_mismatch",
                        ledger_qty=ledger_qty,
                        venue_qty=venue_qty,
                        ledger_entry=position.avg_entry,
                        venue_entry=leg.entry,
                        ledger_leverage=position.leverage,
                        venue_leverage=leg.leverage,
                    )
                )
        return drifts


def _entry_differs(left: Decimal, right: Decimal) -> bool:
    """True when the two entry prices differ by more than the tolerance."""
    if right == 0:
        return True
    return abs(left - right) / right * _HUNDRED > ENTRY_TOLERANCE_PCT


def _normalise(qty: Decimal) -> Decimal:
    return qty.quantize(_QUANTUM)


def _aggregate(legs: Sequence[VenueLeg]) -> dict[str, _VenueBook]:
    """Fold legs into one signed exposure per symbol.

    Hedge mode can hold a LONG and a SHORT leg at once; the ledger carries one signed
    quantity, so the legs are summed with their direction. Entry and leverage are only
    reported when there is a single leg, because averaging them across a hedged pair
    would invent a position that does not exist.
    """
    totals: dict[str, Decimal] = {}
    first: dict[str, VenueLeg] = {}
    counts: dict[str, int] = {}
    for leg in legs:
        totals[leg.symbol] = totals.get(leg.symbol, _ZERO) + _signed(leg)
        first.setdefault(leg.symbol, leg)
        counts[leg.symbol] = counts.get(leg.symbol, 0) + 1
    return {
        symbol: _VenueBook(
            qty=qty,
            entry=first[symbol].entry_price if counts[symbol] == 1 else None,
            leverage=first[symbol].leverage if counts[symbol] == 1 else None,
            legs=counts[symbol],
        )
        for symbol, qty in totals.items()
    }


def _signed(leg: VenueLeg) -> Decimal:
    """``FuturesPosition.qty`` is unsigned; the leg's side carries the direction."""
    side_name = str(getattr(leg.position_side, "value", leg.position_side))
    if side_name.upper() == "SHORT":
        return -abs(leg.qty)
    return abs(leg.qty)
