"""Re-arm the venue-side stop after a restart.

Persisting the stop level (revision 0005) told a restarted process *what* the stop
should be. It could not tell whether that stop still existed: an operator who cancelled
it in the exchange UI, or a venue-side expiry, left a position that looked protected on
the dashboard and was naked in reality.

This module closes that gap. On startup each open position is checked against the venue:

* the recorded ``stop_order_id`` is still working -> nothing to do;
* it is gone (or was never recorded) -> any leftover protection orders for the symbol
  are cancelled so the leg cannot end up with two stops, the exits are placed again, the
  new ids are persisted, and a WARNING says what happened;
* the position has no stop *level* at all -> CRITICAL: there is nothing to re-arm and a
  human has to decide;
* the level is on the wrong side of the mark -> nothing is sent: such a stop would
  trigger at once, so it is reported (``stop_level_invalid``) instead of placed.

The stop matters more than the target, so a missing target alone is logged, not
re-placed: submitting a fresh stop to recover a target would be a worse trade.

The check is not a startup ritual: it runs on every reconciliation cycle as well, so a
stop that disappears mid-session is noticed within one cycle. A stop that is gone while
the position is still open is the signature of a **partial fill** (``closePosition``
orders are one-shot) or of an operator cancelling it at the venue; both leave the
remainder naked, which is exactly what this module re-arms.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, cast

from aegis_alerting import Alert, Alerter, AlertLevel
from aegis_core import Position, PositionSide, Side, utc_now
from aegis_execution import ExchangeError
from aegis_ledger import (
    AuditEvent,
    EventType,
    LedgerWriter,
    PortfolioLedger,
    PositionProtection,
)
from aegis_observability import get_logger

__all__ = ["ProtectionAlertGate", "ProtectionReArmer", "ReArmResult", "VenueProtection"]


def _behind_mark(stop_loss: Decimal, side: Side, mark: Decimal) -> bool:
    """True when the stop is not on the protective side of the current price."""
    return stop_loss >= mark if side is Side.BUY else stop_loss <= mark


class ProtectionAlertGate:
    """Suppress a repeat of the same alert for the same symbol.

    The check runs every reconciliation cycle, so an unprotected position would page the
    operator once a minute forever. Only a *change* is worth a new alert; each cycle is
    still logged at its own level, so nothing is hidden from the log tail.
    """

    def __init__(self) -> None:
        self._last: dict[str, tuple[str, str]] = {}

    def allows(self, *, symbol: str, status: str, reason: str) -> bool:
        """True when this outcome differs from the last one reported for ``symbol``."""
        key = (status, reason)
        if self._last.get(symbol) == key:
            return False
        self._last[symbol] = key
        return True


#: A writer factory returns a context manager around the writer for one write, so the
#: position row and its audit event commit together. ``None`` skips persistence.
WriterFactory = Callable[[], AbstractContextManager[LedgerWriter | None]]


class VenueProtection(Protocol):
    """The venue calls re-arming needs; ``BinanceFuturesTestnetBroker`` satisfies it."""

    def fetch_open_order_ids(self, symbol: str) -> set[str]:
        """Ids of the orders still working for ``symbol``."""

    def cancel_protection_orders(self, symbol: str) -> int:
        """Cancel ``STOP_MARKET``/``TAKE_PROFIT_MARKET`` orders for ``symbol``."""

    def submit_protection_order(
        self,
        *,
        symbol: str,
        side: Side,
        position_side: PositionSide,
        stop_loss: Decimal,
        take_profit: Decimal | None = None,
        qty: Decimal | None = None,
    ) -> Any:
        """Place the exits; the result carries ``stop_order_id``/``take_order_id``."""

    def mark_price(self, symbol: str) -> Decimal | None:
        """Current mark of ``symbol``, or ``None`` when the venue cannot say."""


@dataclass(frozen=True)
class ReArmResult:
    """What happened to one position's venue protection."""

    symbol: str
    #: ``intact``, ``rearmed``, ``no_stop_level``, ``unchecked`` or ``failed``.
    status: str
    stop_order_id: str | None = None
    detail: str = ""
    #: Short code for the outcome (``stop_gone_position_open``, ``rearm_failed``, ...).
    reason: str = ""

    @property
    def healthy(self) -> bool:
        return self.status in {"intact", "rearmed"}


class ProtectionReArmer:
    """Verifies — and when necessary re-places — the stop of every open position."""

    def __init__(
        self,
        *,
        ledger: PortfolioLedger,
        venue: VenueProtection,
        writer_factory: WriterFactory,
        alerter: Alerter | None = None,
        clock: Callable[[], datetime] = utc_now,
        alert_gate: ProtectionAlertGate | None = None,
    ) -> None:
        self._ledger = ledger
        self._venue = venue
        self._writer_factory = writer_factory
        self._alerter = alerter
        self._clock = clock
        self._alert_gate = alert_gate
        self._logger = get_logger("aegis.protection_rearm")

    def run(self) -> tuple[ReArmResult, ...]:
        """Check every open position once, in symbol order."""
        results: list[ReArmResult] = []
        for position in sorted(self._ledger.positions, key=lambda item: item.symbol):
            if position.is_flat:
                continue
            results.append(self._check(position))
        return tuple(results)

    # ------------------------------------------------------------- internals

    def _check(self, position: Position) -> ReArmResult:
        if position.stop_loss is None:
            # Nothing to place: only a human can decide where the risk ends.
            self._logger.critical("position_without_stop_level", symbol=position.symbol)
            self._notify(
                AlertLevel.CRITICAL,
                symbol=position.symbol,
                status="no_stop_level",
                reason="missing_stop_level",
                title="Open position has no stop level",
                message=(
                    f"{position.symbol} is open without a stop level; nothing could be "
                    "re-armed. Reconcile this position by hand."
                ),
                context={
                    "symbol": position.symbol,
                    "status": "UNPROTECTED",
                    "qty": str(position.qty),
                    "reason": "missing_stop_level",
                },
            )
            return ReArmResult(
                symbol=position.symbol, status="no_stop_level", reason="missing_stop_level"
            )

        try:
            open_ids = self._venue.fetch_open_order_ids(position.symbol)
        except ExchangeError as exc:
            # Unknown is not "naked": do not cancel and re-place blind.
            self._logger.warning("protection_check_failed", symbol=position.symbol, error=str(exc))
            return ReArmResult(
                symbol=position.symbol,
                status="unchecked",
                stop_order_id=position.stop_order_id,
                detail=str(exc),
                reason="venue_unreachable",
            )

        if position.stop_order_id is not None and position.stop_order_id in open_ids:
            if position.take_profit is not None and (
                position.tp_order_id is None or position.tp_order_id not in open_ids
            ):
                self._logger.warning(
                    "protection_take_profit_missing",
                    symbol=position.symbol,
                    tp_order_id=position.tp_order_id,
                    detail="the stop is working; the target is not and was left alone",
                )
            self._logger.info(
                "protection_intact",
                symbol=position.symbol,
                stop_order_id=position.stop_order_id,
            )
            return ReArmResult(
                symbol=position.symbol,
                status="intact",
                stop_order_id=position.stop_order_id,
                reason="stop_working",
            )

        if position.stop_order_id is None:
            return self._rearm(
                position,
                reason="no stop order was ever recorded",
                code="no_stop_order_recorded",
            )
        # The position is open and its recorded stop is not: the venue filled it
        # partially (a ``closePosition`` order is one-shot) or somebody cancelled it.
        # Either way the remainder is naked until this re-arms.
        self._logger.warning(
            "protection_stop_missing_with_open_position",
            symbol=position.symbol,
            stop_order_id=position.stop_order_id,
            qty=str(position.qty),
            detail="partial fill or a cancelled stop; re-arming the remaining quantity",
        )
        return self._rearm(
            position,
            reason="the recorded stop is gone while the position is still open",
            code="stop_gone_position_open",
        )

    def _rearm(self, position: Position, *, reason: str, code: str) -> ReArmResult:
        side = position.side
        if side is None or position.stop_loss is None:  # pragma: no cover - caller checks
            return ReArmResult(
                symbol=position.symbol,
                status="unchecked",
                detail=reason,
                reason="venue_unreachable",
            )

        # A level on the wrong side of the mark would trigger the moment the venue
        # accepts it, and re-placing it every cycle would look like protection while
        # behaving like a market exit. Refuse, and leave the venue untouched: cancelling
        # a working target to then place nothing would make things worse.
        mark = self._mark(position.symbol)
        if mark is not None and _behind_mark(position.stop_loss, side, mark):
            self._logger.error(
                "stop_level_invalid",
                symbol=position.symbol,
                side=side.value,
                stop_loss=str(position.stop_loss),
                mark_price=str(mark),
                detail="the stop is on the wrong side of the mark; the venue was not touched",
            )
            self._notify(
                AlertLevel.CRITICAL,
                symbol=position.symbol,
                status="invalid_level",
                reason="stop_level_invalid",
                title="Stop level invalid: not re-armed",
                message=(
                    f"{position.symbol} stop {position.stop_loss} is on the wrong side "
                    f"of mark {mark}; check it by hand."
                ),
                context={
                    "symbol": position.symbol,
                    "status": "UNPROTECTED",
                    "reason": "stop_level_invalid",
                    "side": side.value,
                    "stop_loss": str(position.stop_loss),
                    "mark_price": str(mark),
                },
            )
            return ReArmResult(
                symbol=position.symbol,
                status="invalid_level",
                stop_order_id=position.stop_order_id,
                detail=(f"stop {position.stop_loss} is not on the protective side of mark {mark}"),
                reason="stop_level_invalid",
            )

        # Cancel first: the leg must never hold two stops, and a stale target that
        # outlived its level would close the position at a price nobody chose.
        try:
            cancelled = self._venue.cancel_protection_orders(position.symbol)
        except ExchangeError as exc:
            self._logger.error(
                "protection_cancel_before_rearm_failed",
                symbol=position.symbol,
                error=str(exc),
            )
            self._notify(
                AlertLevel.CRITICAL,
                symbol=position.symbol,
                status="failed",
                reason="cancel_before_rearm_failed",
                title="Cannot re-arm stop: venue group not cleaned up",
                message=(
                    f"{position.symbol} has no working stop and the stale protection "
                    "orders could not be cancelled; re-placing would duplicate them."
                ),
                context={
                    "symbol": position.symbol,
                    "status": "UNPROTECTED",
                    "reason": "cancel_before_rearm_failed",
                    "detail": str(exc)[:400],
                },
            )
            return ReArmResult(
                symbol=position.symbol,
                status="failed",
                detail=str(exc),
                reason="cancel_before_rearm_failed",
            )

        try:
            placed = self._venue.submit_protection_order(
                symbol=position.symbol,
                side=side.opposite,
                position_side=PositionSide.for_entry(side),
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
                qty=position.abs_qty,
            )
        except ExchangeError as exc:
            self._logger.error("protection_rearm_failed", symbol=position.symbol, error=str(exc))
            self._notify(
                AlertLevel.CRITICAL,
                symbol=position.symbol,
                status="failed",
                reason="rearm_failed",
                title="Stop re-arm failed: position is unprotected",
                message=(
                    f"{position.symbol} is open and the venue refused its stop; only the "
                    "local monitor protects it until this is fixed."
                ),
                context={
                    "symbol": position.symbol,
                    "status": "UNPROTECTED",
                    "reason": "rearm_failed",
                    "detail": str(exc)[:400],
                },
            )
            return ReArmResult(
                symbol=position.symbol, status="failed", detail=str(exc), reason="rearm_failed"
            )

        stop_order_id = getattr(placed, "stop_order_id", None)
        tp_order_id = getattr(placed, "take_order_id", None)
        updated = self._ledger.set_protection(
            PositionProtection(
                symbol=position.symbol,
                side=side,
                qty=position.abs_qty,
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
                stop_order_id=stop_order_id,
                tp_order_id=tp_order_id,
            )
        )
        event = AuditEvent(
            event_type=EventType.PROTECTION_REARMED,
            entity_type="position",
            entity_id=position.symbol,
            actor="protection_rearm",
            payload={
                "symbol": position.symbol,
                "reason": reason,
                "reason_code": code,
                "cancelled_orders": cancelled,
                "previous_stop_order_id": position.stop_order_id,
                "stop_order_id": stop_order_id,
                "tp_order_id": tp_order_id,
                "stop_loss": str(position.stop_loss),
                "take_profit": None if position.take_profit is None else str(position.take_profit),
                "checked_at": self._clock().isoformat(),
            },
        )
        with self._writer_factory() as writer:
            if writer is not None:
                writer.save_position(updated)
                writer.append_audit(event)
        self._logger.warning(
            "protection_rearmed",
            symbol=position.symbol,
            reason=reason,
            cancelled=cancelled,
            stop_order_id=stop_order_id,
        )
        self._notify(
            AlertLevel.WARNING,
            symbol=position.symbol,
            status="rearmed",
            reason=code,
            title="Stop re-armed on the venue",
            # The alert body is capped at 128 characters; the reason goes in context.
            message=(
                f"{position.symbol} had no working stop; it was re-placed and the new "
                "order id saved."
            ),
            context={
                "symbol": position.symbol,
                "status": "REARMED",
                "reason": reason,
                "reason_code": code,
                "cancelled_orders": str(cancelled),
                "stop_order_id": stop_order_id,
                "tp_order_id": tp_order_id,
            },
        )
        return ReArmResult(
            symbol=position.symbol,
            status="rearmed",
            stop_order_id=stop_order_id,
            detail=reason,
            reason=code,
        )

    def _mark(self, symbol: str) -> Decimal | None:
        """Ask the venue for the mark; unknown means "do not block the re-arm".

        Leaving a confirmed-naked position unprotected because one price read failed
        would be the worse outcome, so an unreachable mark only skips the sanity check.
        """
        method = getattr(self._venue, "mark_price", None)
        if not callable(method):
            return None
        try:
            return cast(Decimal | None, method(symbol))
        except ExchangeError as exc:
            self._logger.warning("protection_mark_unavailable", symbol=symbol, error=str(exc))
            return None

    def _notify(
        self,
        level: AlertLevel,
        *,
        symbol: str,
        status: str,
        reason: str,
        title: str,
        message: str,
        context: dict[str, Any],
    ) -> None:
        if self._alerter is None:
            return
        if self._alert_gate is not None and not self._alert_gate.allows(
            symbol=symbol, status=status, reason=reason
        ):
            self._logger.info(
                "protection_alert_suppressed", symbol=symbol, status=status, reason=reason
            )
            return
        self._alerter.send(
            Alert(
                level=level,
                title=title,
                message=message,
                source="protection_rearm",
                context=context,
            )
        )
