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

from sqlalchemy.exc import SQLAlchemyError

from aegis_alerting import Alert, Alerter, AlertLevel
from aegis_core import Position, Side, Trade, utc_now
from aegis_execution import ExchangeError
from aegis_ledger import PortfolioLedger
from aegis_observability import get_logger

__all__ = [
    "PROTECTION_STRATEGY_ID",
    "DriftStateWriter",
    "PositionDrift",
    "PositionReconciler",
    "VenueFill",
    "VenueFillFeed",
    "VenueFillRecorder",
    "VenueLeg",
    "VenuePositionFeed",
]

#: ``strategy_id`` of a trade booked from an exit the venue executed for us. It is
#: deliberately not the strategy that opened the position: nothing in the strategy
#: decided this, the stop did.
PROTECTION_STRATEGY_ID = "venue_protection"
#: Drifts a completed protective exit can explain: the book still holds exposure the
#: venue already closed, in whole or in part.
_CLOSURE_KINDS = frozenset({"missing_at_venue", "qty_mismatch"})

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


@dataclass(frozen=True)
class VenueFill:
    """One execution the venue reports for an order we never booked locally."""

    order_id: str
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    filled_at: datetime
    fee: Decimal = _ZERO
    order_type: str = "STOP_MARKET"


class VenuePositionFeed(Protocol):
    """The one venue call this job makes."""

    def fetch_positions(self) -> Sequence[VenueLeg]:
        """Every open leg, as the exchange sees it."""


class VenueFillFeed(Protocol):
    """History of the venue's own protective exits for one symbol."""

    def fetch_protection_fills(
        self, symbol: str, *, since: datetime | None = None
    ) -> Sequence[VenueFill]:
        """``STOP_MARKET``/``TAKE_PROFIT_MARKET`` executions, oldest first."""


class VenueFillRecorder(Protocol):
    """Durably books the fills that explained a gap (trade, position, audit)."""

    def record_venue_fills(
        self, *, symbol: str, trades: Sequence[Trade], position: Position
    ) -> None:
        """Persist the trades and the resulting position in one transaction."""


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
        fills: VenueFillFeed | None = None,
        recorder: VenueFillRecorder | None = None,
    ) -> None:
        self._ledger = ledger
        self._venue = venue
        self._state = state
        self._alerter = alerter
        self._clock = clock
        #: Without both of these, every disagreement stays a drift (the safe default).
        self._fills = fills
        self._recorder = recorder
        self._logger = get_logger("aegis.position_reconciler")

    def run(self) -> tuple[PositionDrift, ...]:
        """One pass. An unreachable venue leaves the previous flag untouched."""
        try:
            legs = self._venue.fetch_positions()
        except ExchangeError as exc:
            # Silence is not agreement: do not mark the book synced on a failed read.
            self._logger.warning("position_reconcile_fetch_failed", error=str(exc))
            return ()

        drifts = [drift for drift in self.compare(legs) if not self._explain(drift)]
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

    def _explain(self, drift: PositionDrift) -> bool:
        """Book the venue's protective exit when it accounts for the missing exposure.

        A stop that fired on the exchange is *not* a ledger bug: the venue did what it
        was told and our book simply never heard about it. Halt-ing entries over that
        would be as wrong as ignoring a real drift, so the fills are replayed first and
        only an unexplained remainder is reported.
        """
        if drift.kind not in _CLOSURE_KINDS or self._fills is None or self._recorder is None:
            return False
        position = self._ledger.position(drift.symbol)
        if position.is_flat:
            # Nothing of ours to close: a venue-only position cannot be a stop fill.
            return False
        try:
            fills = self._fills.fetch_protection_fills(drift.symbol, since=position.opened_at)
        except ExchangeError as exc:
            self._logger.warning("venue_fill_lookup_failed", symbol=drift.symbol, error=str(exc))
            return False

        planned = _plan_trades(position, fills, target_qty=drift.venue_qty)
        if planned is None:
            return False
        trades, resulting = planned
        try:
            self._recorder.record_venue_fills(
                symbol=drift.symbol, trades=trades, position=resulting
            )
        except SQLAlchemyError as exc:
            # Recording is the whole point: a fill that could not be saved must still
            # be reported as a drift, not silently applied to the in-memory book.
            self._logger.error("venue_fill_record_failed", symbol=drift.symbol, error=str(exc))
            return False
        for trade in trades:
            self._ledger.apply_trade(trade)
        self._logger.warning(
            "venue_fill_reconciled",
            symbol=drift.symbol,
            trades=len(trades),
            booked_qty=str(sum((trade.qty for trade in trades), _ZERO)),
            venue_qty=str(drift.venue_qty),
            detail="protective exit executed at the venue; booked as venue_protection",
        )
        return True

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


def _plan_trades(
    position: Position, fills: Sequence[VenueFill], *, target_qty: Decimal
) -> tuple[list[Trade], Position] | None:
    """Replay ``fills`` and accept them only if they land exactly on the venue's qty.

    Anything that would grow or flip the position, and any total that does not equal
    what the venue reports, is refused: a fabricated trade is worse than a drift,
    because it silently rewrites the record a human is supposed to inspect.
    """
    direction = Decimal(1) if position.qty > 0 else Decimal(-1)
    scratch = position
    trades: list[Trade] = []
    for index, fill in enumerate(fills):
        if fill.side.sign * direction >= 0:
            continue  # increases exposure: not an exit
        signed = fill.qty * Decimal(fill.side.sign)
        if fill.qty <= 0 or fill.price <= 0 or abs(signed) > abs(scratch.qty):
            return None
        scratch = scratch.apply_fill(signed, fill.price, leverage=position.leverage)
        trades.append(
            Trade(
                trade_id=f"venue-{fill.order_id}-{index}"[:64],
                order_id=f"venue:{fill.order_id}",
                symbol=position.symbol,
                side=fill.side,
                qty=fill.qty,
                price=fill.price,
                fee=fill.fee,
                filled_at=fill.filled_at,
                strategy_id=PROTECTION_STRATEGY_ID,
                leverage=position.leverage,
            )
        )
    if not trades or _normalise(scratch.qty) != _normalise(target_qty):
        return None
    return trades, scratch


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
