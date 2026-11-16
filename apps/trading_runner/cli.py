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
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from aegis_config import Settings, load_settings
from aegis_db import create_engine_from_settings, create_session_factory, session_scope
from aegis_ledger import LedgerError, LedgerStore, LedgerWriter, PortfolioLedger
from aegis_observability import configure_logging, get_logger
from aegis_risk import RiskGate
from position_monitor.quotes import CandleQuoteSource
from signal_service import SignalService, SqlCandleRepository
from trading_runner.candles import SqlLatestCandleSource
from trading_runner.runner import RunnerReport, TradingRunner, ensure_safe_execution_mode

__all__ = ["build_runner", "main", "report_payload", "restore_ledger"]


@contextmanager
def _writer(factory: sessionmaker[Session]) -> Iterator[LedgerWriter]:
    """One transaction per tick: order, trade, position and audit commit together."""
    with session_scope(factory) as session:
        yield LedgerStore(session)


def restore_ledger(ledger: PortfolioLedger, factory: sessionmaker[Session]) -> None:
    """Reload positions and realized PnL from the database (best effort, logged)."""
    logger = get_logger("aegis.runner")
    try:
        with session_scope(factory) as session:
            summary = ledger.reconstruct_from_db(session)
    except (LedgerError, SQLAlchemyError) as exc:
        logger.warning("runner_ledger_reconstruction_failed", error=str(exc))
        return
    logger.info(
        "runner_ledger_reconstructed",
        trades=summary.trades_replayed,
        open_positions=summary.open_positions,
        equity=str(summary.equity),
        daily_realized_pnl=str(summary.daily_realized_pnl),
    )


def build_runner(settings: Settings, factory: sessionmaker[Session]) -> TradingRunner:
    """Wire the runner: real gate, paper/live broker per config, SQL candles."""
    ledger = PortfolioLedger(initial_equity=settings.risk.initial_equity)
    restore_ledger(ledger, factory)
    signals = SignalService(
        gate=RiskGate.from_settings(settings),
        ledger=ledger,
        candles=SqlCandleRepository(factory),
        symbols=settings.market_data.symbols,
        timeframe=settings.market_data.timeframes[0],
    )
    return TradingRunner(
        settings=settings,
        ledger=ledger,
        signals=signals,
        candles=SqlLatestCandleSource(factory),
        quotes=CandleQuoteSource(factory),
        writer_factory=lambda: _writer(factory),
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
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.observability)
    logger = get_logger("aegis.runner")
    ensure_safe_execution_mode(settings)

    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)
    runner = build_runner(settings, factory)

    try:
        if args.once:
            print(json.dumps(report_payload(runner.run_once()), sort_keys=True))
            return 0

        lock = threading.Lock()

        def entry_job() -> None:
            with lock:
                runner.run_entry_cycle()

        def monitor_job() -> None:
            with lock:
                runner.run_monitor_cycle()

        scheduler = BlockingScheduler(timezone="UTC")
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
        logger.info(
            "runner_started",
            environment=settings.environment.value,
            execution_mode=settings.execution.mode.value,
            signal_interval_seconds=settings.runner.signal_interval_seconds,
            monitor_interval_seconds=settings.runner.monitor_interval_seconds,
            monitor_enabled=settings.runner.monitor_enabled and not args.no_monitor,
            kill_switch=settings.risk.kill_switch,
        )
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("runner_stopped")
    finally:
        engine.dispose()

    return 0
