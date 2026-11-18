"""Order reconciler command line.

    python -m order_reconciler --once     # one pass, then exit
    python -m order_reconciler            # loop every reconciler.interval_seconds

Only meaningful for a real adapter: with the paper broker every order is decided
synchronously, so the CLI says so and exits instead of polling an empty queue.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, cast

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from aegis_alerting import Alerter, build_alerter
from aegis_config import Settings, load_settings
from aegis_core import utc_now
from aegis_db import create_engine_from_settings, create_session_factory, session_scope
from aegis_execution import ExchangeOrderState, build_broker
from aegis_ledger import AuditEvent, EventType, LedgerError, LedgerStore, PortfolioLedger
from aegis_observability import configure_logging, get_logger
from order_reconciler.fills import SqlVenueFillRecorder
from order_reconciler.positions import PositionDrift, PositionReconciler, VenueFill, VenueLeg
from order_reconciler.protection import (
    ProtectionAlertGate,
    ProtectionReArmer,
    ReArmResult,
    VenueProtection,
)
from order_reconciler.reconciler import OrderReconciler, ReconciliationResult
from order_reconciler.repository import SqlOrderBook
from order_reconciler.state import SqlReconciliationState

__all__ = [
    "acknowledge_drift",
    "build_reconciler",
    "main",
    "rearm_protections",
    "reconcile_once",
    "reconcile_positions_once",
    "result_payload",
]

#: What a broker must expose before its protection orders can be verified or re-placed.
_PROTECTION_CALLS = ("fetch_open_order_ids", "cancel_protection_orders", "submit_protection_order")


def _check_protection(
    broker: Any,
    factory: sessionmaker[Session],
    ledger: PortfolioLedger,
    *,
    alerter: Alerter | None,
    alert_gate: ProtectionAlertGate | None = None,
) -> tuple[ReArmResult, ...]:
    """Verify every open position's venue stop once, re-placing what is gone.

    Shared by the startup check and by every reconciliation cycle, so the two paths can
    never drift apart: the position row says a stop *should* exist, and only the venue
    says whether it still does.
    """
    logger = get_logger("aegis.protection_rearm")
    if not all(callable(getattr(broker, name, None)) for name in _PROTECTION_CALLS):
        logger.info(
            "protection_rearm_skipped",
            reason="broker cannot hold protection orders (spot does not need this)",
        )
        return ()
    rearmer = ProtectionReArmer(
        ledger=ledger,
        venue=cast(VenueProtection, broker),
        writer_factory=lambda: _ledger_writer(factory),
        alerter=alerter,
        alert_gate=alert_gate,
    )
    results = rearmer.run()
    for result in results:
        logger.info(
            "protection_check",
            symbol=result.symbol,
            status=result.status,
            reason=result.reason,
            stop_order_id=result.stop_order_id,
        )
    return results


class _BrokerFeed:
    """Adapts a broker to ``ExchangeOrderFeed`` (fetch only)."""

    def __init__(self, broker: Any) -> None:
        self._broker = broker

    def fetch(self, exchange_order_id: str, *, symbol: str) -> ExchangeOrderState:
        return cast(ExchangeOrderState, self._broker.fetch(exchange_order_id, symbol=symbol))


class _PositionFeed:
    """Adapts a futures broker to ``VenuePositionFeed`` and ``VenueFillFeed``."""

    def __init__(self, broker: Any) -> None:
        self._broker = broker

    def fetch_positions(self) -> tuple[VenueLeg, ...]:
        return cast(tuple[VenueLeg, ...], self._broker.fetch_positions())

    def fetch_protection_fills(self, symbol: str, *, since: Any = None) -> tuple[VenueFill, ...]:
        """Empty for a broker that cannot report fills: the gap stays a drift."""
        method = getattr(self._broker, "fetch_protection_fills", None)
        if not callable(method):
            return ()
        return cast(tuple[VenueFill, ...], tuple(method(symbol, since=since)))


@contextmanager
def _ledger_writer(factory: sessionmaker[Session]) -> Iterator[LedgerStore]:
    """One transaction for a position row plus its audit event."""
    with session_scope(factory) as session:
        yield LedgerStore(session)


def build_reconciler(
    settings: Settings, session: Session, broker: Any, ledger: PortfolioLedger
) -> OrderReconciler:
    """Wire one pass against the caller's transaction."""
    return OrderReconciler(
        ledger=ledger,
        orders=SqlOrderBook(session),
        exchange=_BrokerFeed(broker),
        writer=LedgerStore(session),
    )


def result_payload(result: ReconciliationResult) -> dict[str, Any]:
    return {
        "order_id": result.order_id,
        "symbol": result.symbol,
        "previous": result.previous.value,
        "current": result.current.value,
        "filled_delta": str(result.filled_delta),
        "action": result.action,
        "trade_id": None if result.trade is None else result.trade.trade_id,
    }


def _restore_ledger(ledger: PortfolioLedger, factory: sessionmaker[Session]) -> None:
    logger = get_logger("aegis.order_reconciler")
    try:
        with session_scope(factory) as session:
            summary = ledger.reconstruct_from_db(session)
    except (LedgerError, SQLAlchemyError) as exc:
        logger.warning("reconciler_ledger_reconstruction_failed", error=str(exc))
        return
    logger.info("reconciler_ledger_reconstructed", trades=summary.trades_replayed)


def reconcile_once(
    settings: Settings, factory: sessionmaker[Session], ledger: PortfolioLedger
) -> tuple[ReconciliationResult, ...]:
    """Reconcile every open order once, closing the broker it built.

    Used by the daemon itself (``--once`` loop) and by ``trading_runner`` at startup,
    so orders that filled or cancelled while the process was down are recorded before
    the first tick can consider the book final.
    """
    broker = build_broker(
        settings.execution, exchange=settings.exchange, live_confirm=settings.live_confirm
    )
    try:
        with session_scope(factory) as session:
            return build_reconciler(settings, session, broker, ledger).run()
    finally:
        close = getattr(broker, "close", None)
        if callable(close):
            close()


def reconcile_positions_once(
    settings: Settings,
    factory: sessionmaker[Session],
    ledger: PortfolioLedger,
    *,
    broker: Any = None,
    alerter: Alerter | None = None,
    check_protection: bool = True,
    alert_gate: ProtectionAlertGate | None = None,
) -> tuple[PositionDrift, ...]:
    """Compare the book with the venue once, recording the verdict durably.

    Paper has no venue to disagree with, so it is skipped rather than reported as
    synced; a broker without ``fetch_positions`` (spot) is skipped for the same reason.

    The protection check runs *after* the comparison and on the same broker: a venue
    fill has to reach the book before the remaining quantity is re-armed, otherwise the
    stop would be sized and levelled against exposure that no longer exists.
    """
    logger = get_logger("aegis.position_reconciler")
    if settings.execution.broker == "paper":
        return ()

    owns_broker = broker is None
    if broker is None:
        broker = build_broker(
            settings.execution, exchange=settings.exchange, live_confirm=settings.live_confirm
        )
    try:
        if not callable(getattr(broker, "fetch_positions", None)):
            logger.info(
                "position_reconcile_skipped",
                reason="broker exposes no positions (spot does not need this)",
            )
            return ()
        feed = _PositionFeed(broker)
        reconciler = PositionReconciler(
            ledger=ledger,
            venue=feed,
            state=SqlReconciliationState(factory),
            alerter=alerter if alerter is not None else build_alerter(settings.alerting),
            # A stop that fired on the venue is booked, not reported as drift.
            fills=feed,
            recorder=SqlVenueFillRecorder(factory),
        )
        drifts = reconciler.run()
        if drifts:
            with session_scope(factory) as session:
                store = LedgerStore(session)
                for drift in drifts:
                    store.append_audit(
                        AuditEvent(
                            event_type=EventType.POSITION_DRIFT_DETECTED,
                            entity_type="position",
                            entity_id=drift.symbol,
                            actor="position_reconciler",
                            payload=drift.as_payload(),
                        )
                    )
        if check_protection:
            _check_protection(
                broker,
                factory,
                ledger,
                alerter=alerter if alerter is not None else build_alerter(settings.alerting),
                alert_gate=alert_gate,
            )
        return drifts
    finally:
        if owns_broker:
            close = getattr(broker, "close", None)
            if callable(close):
                close()


def rearm_protections(
    settings: Settings,
    factory: sessionmaker[Session],
    ledger: PortfolioLedger,
    *,
    alerter: Alerter | None = None,
    alert_gate: ProtectionAlertGate | None = None,
) -> tuple[ReArmResult, ...]:
    """Verify the venue still holds every open position's stop, and re-place it if not.

    Kept as the standalone entry point (``--once`` runs, tests and operators); the
    runner also calls the same check from every reconciliation cycle.
    """
    if settings.execution.broker == "paper":
        return ()

    broker = build_broker(
        settings.execution, exchange=settings.exchange, live_confirm=settings.live_confirm
    )
    try:
        return _check_protection(
            broker,
            factory,
            ledger,
            alerter=alerter if alerter is not None else build_alerter(settings.alerting),
            alert_gate=alert_gate,
        )
    finally:
        close = getattr(broker, "close", None)
        if callable(close):
            close()


def acknowledge_drift(settings: Settings, factory: sessionmaker[Session]) -> bool:
    """Clear the halt flag after an operator has reconciled the book by hand.

    Returns ``True`` when a halt was actually cleared. The clearance is audited, because
    "a human said it was fine" is exactly the kind of decision that must be traceable.
    """
    store = SqlReconciliationState(factory)
    snapshot = store.load()
    if not snapshot.entries_halted:
        return False
    now = utc_now()
    store.acknowledge(now)
    with session_scope(factory) as session:
        LedgerStore(session).append_audit(
            AuditEvent(
                event_type=EventType.POSITION_DRIFT_ACKNOWLEDGED,
                entity_type="reconciliation_state",
                entity_id="singleton",
                actor="operator",
                payload={
                    "drift_count": snapshot.drift_count,
                    "detail": list(snapshot.detail),
                    "cleared_at": now.isoformat(),
                },
            )
        )
    get_logger("aegis.position_reconciler").warning(
        "position_drift_acknowledged", drift_count=snapshot.drift_count
    )
    _ = settings
    return True


def _run_once(
    settings: Settings, factory: sessionmaker[Session], broker: Any, ledger: PortfolioLedger
) -> tuple[ReconciliationResult, ...]:
    with session_scope(factory) as session:
        return build_reconciler(settings, session, broker, ledger).run()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AegisTrade order reconciler")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument(
        "--acknowledge-drift",
        action="store_true",
        help="clear the entries-halted flag after reconciling the book by hand",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.observability)
    logger = get_logger("aegis.order_reconciler")

    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)

    if args.acknowledge_drift:
        cleared = acknowledge_drift(settings, factory)
        logger.warning(
            "position_drift_acknowledge_requested",
            cleared=cleared,
            detail=(
                "entries are allowed again; the next comparison re-checks the venue"
                if cleared
                else "no drift was blocking entries"
            ),
        )
        engine.dispose()
        return 0

    if settings.execution.broker == "paper":
        logger.warning(
            "reconciler_skipped",
            detail="paper broker fills synchronously; there is nothing to reconcile",
        )
        engine.dispose()
        return 0

    ledger = PortfolioLedger(initial_equity=settings.risk.initial_equity)
    _restore_ledger(ledger, factory)
    logger.info(
        "reconciler_started",
        broker=settings.execution.broker,
        interval_seconds=settings.reconciler.interval_seconds,
        position_interval_seconds=settings.reconciler.position_interval_seconds,
    )
    position_interval = settings.reconciler.position_interval_seconds
    next_position_check = 0.0
    try:
        while True:
            results = reconcile_once(settings, factory, ledger)
            changed = [result for result in results if result.action != "unchanged"]
            if results:
                print(
                    json.dumps([result_payload(item) for item in changed], sort_keys=True),
                    file=sys.stdout,
                )
            # Orders poll every few seconds; the position comparison has its own,
            # slower cadence (one minute by default) and its own cost.
            if time.monotonic() >= next_position_check:
                drifts = reconcile_positions_once(settings, factory, ledger)
                next_position_check = time.monotonic() + position_interval
                if drifts:
                    print(
                        json.dumps([drift.as_payload() for drift in drifts], sort_keys=True),
                        file=sys.stdout,
                    )
            if args.once:
                return 0
            time.sleep(settings.reconciler.interval_seconds)
    except KeyboardInterrupt:
        logger.info("reconciler_stopped")
    finally:
        engine.dispose()
    return 0
