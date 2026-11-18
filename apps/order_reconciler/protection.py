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
  human has to decide.

The stop matters more than the target, so a missing target alone is logged, not
re-placed: submitting a fresh stop to recover a target would be a worse trade.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

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

__all__ = ["ProtectionReArmer", "ReArmResult", "VenueProtection"]

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


@dataclass(frozen=True)
class ReArmResult:
    """What happened to one position's venue protection."""

    symbol: str
    #: ``intact``, ``rearmed``, ``no_stop_level``, ``unchecked`` or ``failed``.
    status: str
    stop_order_id: str | None = None
    detail: str = ""

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
    ) -> None:
        self._ledger = ledger
        self._venue = venue
        self._writer_factory = writer_factory
        self._alerter = alerter
        self._clock = clock
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
            return ReArmResult(symbol=position.symbol, status="no_stop_level")

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
            )

        reason = (
            "no stop order was ever recorded"
            if position.stop_order_id is None
            else "the recorded stop is no longer open on the venue"
        )
        return self._rearm(position, reason=reason)

    def _rearm(self, position: Position, *, reason: str) -> ReArmResult:
        side = position.side
        if side is None or position.stop_loss is None:  # pragma: no cover - caller checks
            return ReArmResult(symbol=position.symbol, status="unchecked", detail=reason)

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
            return ReArmResult(symbol=position.symbol, status="failed", detail=str(exc))

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
            return ReArmResult(symbol=position.symbol, status="failed", detail=str(exc))

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
            title="Stop re-armed on the venue",
            message=(
                f"{position.symbol} had no working stop ({reason}); it was re-placed and "
                "its order id saved."
            ),
            context={
                "symbol": position.symbol,
                "status": "REARMED",
                "reason": reason,
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
        )

    def _notify(
        self, level: AlertLevel, *, title: str, message: str, context: dict[str, Any]
    ) -> None:
        if self._alerter is None:
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
