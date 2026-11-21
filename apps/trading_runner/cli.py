"""Autonomous runner command line.

    python -m trading_runner --once     # one entry scan + one monitor pass, then exit
    python -m trading_runner            # daemon: entries every 5 min, monitor every 60 s

The daemon schedules two jobs on one ``BlockingScheduler``. They share a lock, because
both mutate the same in-process ledger; a slow signal scan therefore delays a
stop-loss check by at most its own runtime instead of corrupting the book.

Startup refuses to continue when ``execution.mode`` is ``live`` while
``execution.allow_live_orders`` is false (``RunnerConfigError``), and the ledger is
rebuilt from ``trading.trades`` before the first tick so risk sees the real book.
"""

from __future__ import annotations

import argparse
import json
import signal
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from prometheus_client import start_http_server
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from aegis_alerting import Alert, AlertLevel, build_alerter
from aegis_config import Settings, load_settings
from aegis_db import (
    SqlActivityWriter,
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from aegis_ledger import LedgerError, LedgerStore, LedgerWriter, PortfolioLedger
from aegis_observability import Metrics, configure_logging, get_logger
from aegis_observability.heartbeat import touch_heartbeat
from aegis_risk import RiskGate
from aegis_strategy import EmaCrossParams
from order_reconciler.cli import reconcile_once, reconcile_positions_once
from order_reconciler.protection import ProtectionAlertGate
from order_reconciler.state import SqlReconciliationState
from position_monitor.quotes import CandleQuoteSource, live_quote_source
from signal_service import SignalService, SqlCandleRepository, SqlSignalStore
from trading_runner.candles import SqlLatestCandleSource
from trading_runner.runner import RunnerReport, TradingRunner, ensure_safe_execution_mode

__all__ = ["build_runner", "main", "report_payload", "restore_ledger"]


@contextmanager
def _writer(factory: sessionmaker[Session]) -> Iterator[LedgerWriter]:
    """One transaction per tick: order, trade, position and audit commit together."""
    with session_scope(factory) as session:
        yield LedgerStore(session)


def restore_ledger(ledger: PortfolioLedger, factory: sessionmaker[Session]) -> bool:
    """Reload positions and realized PnL from the database.

    Returns ``True`` only when the replay actually ran. That boolean is a safety
    property, not a log detail: a failed replay leaves the book at zero positions and
    zero loss windows, so the daily/weekly limits would silently start from scratch.
    The caller blocks entries when this is false; positions and exits are untouched.
    """
    logger = get_logger("aegis.runner")
    try:
        with session_scope(factory) as session:
            summary = ledger.reconstruct_from_db(session)
    except (LedgerError, SQLAlchemyError) as exc:
        logger.error("runner_ledger_reconstruction_failed", error=str(exc))
        return False
    logger.info(
        "runner_ledger_reconstructed",
        trades=summary.trades_replayed,
        open_positions=summary.open_positions,
        equity=str(summary.equity),
        daily_realized_pnl=str(summary.daily_realized_pnl),
        weekly_realized_pnl=str(summary.weekly_realized_pnl),
    )
    return True


def reconcile_at_startup(
    settings: Settings, factory: sessionmaker[Session], ledger: PortfolioLedger
) -> None:
    """Settle orders that may have filled while the process was down.

    The reconciler is the only writer of late fills, and a daemon that starts with a
    stale book would size the next order against a position it does not know it has.
    A failure here is logged, not fatal: the periodic reconciler keeps trying.
    """
    logger = get_logger("aegis.runner")
    if settings.execution.broker == "paper":
        return
    try:
        results = reconcile_once(settings, factory, ledger)
    except Exception as exc:
        logger.error("runner_startup_reconcile_failed", error=str(exc))
        return
    changed = [result for result in results if result.action != "unchanged"]
    logger.info(
        "runner_startup_reconcile",
        checked=len(results),
        changed=len(changed),
        orders=[result.order_id for result in changed],
    )


def build_runner(
    settings: Settings,
    factory: sessionmaker[Session],
    *,
    metrics: Metrics | None = None,
) -> TradingRunner:
    """Wire the runner: real gate, paper/live broker per config, SQL candles."""
    ledger = PortfolioLedger(initial_equity=settings.risk.initial_equity)
    alerter = build_alerter(settings.alerting)
    restored = restore_ledger(ledger, factory)
    if not restored:
        # Fail closed, loudly. Trading on a zeroed book would size the next entry
        # against equity and loss windows that never included this month's losses.
        alerter.send(
            Alert(
                level=AlertLevel.CRITICAL,
                title="LEDGER NOT RECONSTRUCTED",
                message=(
                    "The database replay failed, so daily/weekly loss windows are unknown. "
                    "New entries are blocked until the runner restarts cleanly."
                ),
                source="runner",
                context={"status": "HALTED", "reason": "ledger_unrestored"},
            )
        )
    else:
        reconcile_at_startup(settings, factory, ledger)
    # A restored position row says a stop *should* exist. Only the venue knows whether
    # it still does, so every reconciliation cycle verifies (and re-places) it — the
    # daemon runs one immediately, before the first entry. The gate remembers what was
    # already alerted, so a naked position pages once instead of once a minute.
    alert_gate = ProtectionAlertGate()
    state = SqlReconciliationState(factory)

    def entries_blocked() -> str | None:
        """Why entries must not run: an unreconstructed book first, then drift."""
        if not restored:
            return "ledger_unrestored"
        try:
            return state.load().blocked_reason
        except SQLAlchemyError as exc:
            # Fail closed: without the flag's answer, the book cannot be trusted.
            get_logger("aegis.runner").warning("reconciliation_state_unavailable", error=str(exc))
            return "reconciliation_unavailable"

    signals = SignalService(
        gate=RiskGate.from_settings(settings),
        ledger=ledger,
        candles=SqlCandleRepository(
            factory,
            environment=settings.exchange.environment,
            venue=settings.execution.market_type.value,
        ),
        symbols=settings.market_data.symbols,
        timeframe=settings.market_data.timeframes[0],
        # Persisted, so a restart continues the same run instead of re-announcing bars.
        store=SqlSignalStore(factory),
        params=EmaCrossParams(
            fast_period=settings.strategy.ema_fast,
            slow_period=settings.strategy.ema_slow,
            adx_threshold=settings.strategy.adx_threshold,
        ),
    )
    return TradingRunner(
        settings=settings,
        ledger=ledger,
        signals=signals,
        candles=SqlLatestCandleSource(
            factory,
            environment=settings.exchange.environment,
            venue=settings.execution.market_type.value,
        ),
        quotes=CandleQuoteSource.from_settings(settings, factory),
        writer_factory=lambda: _writer(factory),
        live_quotes=live_quote_source(settings, factory),
        alerter=alerter,
        entries_blocked=entries_blocked,
        position_reconciliation=lambda: reconcile_positions_once(
            settings, factory, ledger, alert_gate=alert_gate
        ),
        # Its own session per message and its own retention: an activity row must never
        # join — or roll back — the trading transaction it describes.
        activity=SqlActivityWriter(factory),
        metrics=metrics,
    )


def report_payload(report: RunnerReport) -> dict[str, Any]:
    """JSON-ready summary of one ``--once`` pass."""
    return {
        "entries": {
            "skipped": report.entries.skipped,
            "generated": report.entries.generated,
            "converted": list(report.entries.converted),
            "filled": list(report.entries.filled),
            "rejected": list(report.entries.rejected),
        },
        "drift": report.drift,
        "exits": [
            {
                "symbol": action.symbol,
                "trigger": action.trigger.value,
                "filled": action.closed,
                "approved": action.result.decision.approved,
                "reasons": list(action.result.reasons),
            }
            for action in report.exits
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AegisTrade autonomous trading runner")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument(
        "--no-monitor", action="store_true", help="do not schedule the protective-exit job"
    )
    parser.add_argument(
        "--heartbeat-file",
        default=None,
        help="touch this file from a scheduler job so Docker can healthcheck the daemon",
    )
    args = parser.parse_args(argv)
    heartbeat_path: str | None = args.heartbeat_file

    settings = load_settings()
    configure_logging(settings.observability)
    logger = get_logger("aegis.runner")
    ensure_safe_execution_mode(settings)

    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)

    metrics: Metrics | None = None
    if settings.observability.metrics_enabled:
        metrics = Metrics()
        try:
            start_http_server(settings.observability.metrics_port, registry=metrics.registry)
            logger.info("runner_metrics_started", port=settings.observability.metrics_port)
        except OSError as exc:
            # Two processes cannot share a port; a missing endpoint must not stop
            # trading, but it does mean the safety rules scrape nothing from here.
            logger.warning("runner_metrics_unavailable", error=str(exc))
            metrics = None

    runner = build_runner(settings, factory, metrics=metrics)
    if settings.risk.kill_switch:
        build_alerter(settings.alerting).send(
            Alert(
                level=AlertLevel.CRITICAL,
                title="Kill switch active",
                message="The runner started with risk.kill_switch=true; no entries.",
                source="runner",
                context={"status": "HALTED", "reason": "kill_switch"},
            )
        )

    try:
        if args.once:
            if heartbeat_path is not None:
                touch_heartbeat(heartbeat_path)
            print(json.dumps(report_payload(runner.run_once()), sort_keys=True))
            return 0

        lock = threading.Lock()
        stop = threading.Event()

        def _shutdown(signum: int, _frame: Any) -> None:
            # SIGTERM from an orchestrator and Ctrl-C both land here, so the scheduler
            # is stopped after the job in flight rather than mid-write.
            logger.info("runner_shutdown_requested", signal=signum)
            stop.set()

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        def entry_job() -> None:
            with lock:
                runner.run_entry_cycle()

        def monitor_job() -> None:
            with lock:
                runner.run_monitor_cycle()

        def reconcile_job() -> None:
            with lock:
                runner.run_position_reconciliation()

        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(
            entry_job,
            IntervalTrigger(seconds=settings.runner.signal_interval_seconds),
            id="entries",
            max_instances=1,
            coalesce=True,
        )
        if settings.runner.monitor_enabled and not args.no_monitor:
            scheduler.add_job(
                monitor_job,
                IntervalTrigger(seconds=settings.runner.monitor_interval_seconds),
                id="monitor",
                max_instances=1,
                coalesce=True,
            )
        if settings.execution.broker != "paper":
            scheduler.add_job(
                reconcile_job,
                IntervalTrigger(seconds=settings.reconciler.position_interval_seconds),
                id="position-reconcile",
                max_instances=1,
                coalesce=True,
            )
            # Do not wait a minute to learn that the book is already wrong.
            runner.run_position_reconciliation()
        if heartbeat_path is not None:
            # Write once at startup so a container that is still warming up is not
            # reported dead by the first healthcheck, then every ten seconds from the
            # scheduler: if that thread stalls, the file goes stale and Docker notices.
            touch_heartbeat(heartbeat_path)
            scheduler.add_job(
                lambda: touch_heartbeat(heartbeat_path),
                IntervalTrigger(seconds=10),
                id="heartbeat",
                max_instances=1,
                coalesce=True,
            )
        logger.info(
            "runner_started",
            environment=settings.environment.value,
            position_reconcile_interval_seconds=(
                None
                if settings.execution.broker == "paper"
                else settings.reconciler.position_interval_seconds
            ),
            execution_mode=settings.execution.mode.value,
            signal_interval_seconds=settings.runner.signal_interval_seconds,
            monitor_interval_seconds=settings.runner.monitor_interval_seconds,
            monitor_enabled=settings.runner.monitor_enabled and not args.no_monitor,
            kill_switch=settings.risk.kill_switch,
        )
        scheduler.start()
        try:
            while not stop.wait(timeout=1.0):
                pass
        except KeyboardInterrupt:  # pragma: no cover - stop already set by the handler
            pass
        finally:
            scheduler.shutdown(wait=True)
            logger.info("runner_stopped", graceful=True)
    finally:
        engine.dispose()

    return 0
