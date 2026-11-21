"""Position monitor command line.

    python -m position_monitor --once                 # one check, then exit
    python -m position_monitor --interval 15          # loop (SIGINT to stop)
    python -m position_monitor --once --no-restore    # ignore positions in the DB

The monitor runs in its own process, so it loads the open positions, their leverage and
their stop/target levels (with the venue order ids) from ``trading.positions`` at
startup (``--restore``, on by default). Without that it would watch an empty ledger,
because ledger state is not rebuilt from the tables automatically (see
``docs/runbooks/ledger-reconciliation.md``).

Prices come from the newest stored candle, which the collector writes; the monitor
never fetches from an exchange itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aegis_config import Settings, load_settings
from aegis_core import Position
from aegis_db import (
    PositionRecord,
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from aegis_execution import ExecutionGateway, TradingPipeline, build_broker
from aegis_ledger import LedgerStore, LedgerWriter, PortfolioLedger, PositionProtection
from aegis_ledger.records import position_from_row
from aegis_observability import get_logger
from aegis_risk import RiskGate
from order_reconciler.cli import reconcile_positions_once
from position_monitor.monitor import CheckLogCadence, MonitorAction, PositionMonitor
from position_monitor.quotes import CandleQuoteSource, live_quote_source
from position_monitor.venue import BrokerVenueCheck

__all__ = ["action_payload", "book_venue_fills", "build_monitor", "main", "restore_from_database"]


def restore_from_database(
    ledger: PortfolioLedger, factory: sessionmaker[Session]
) -> tuple[Position, ...]:
    """Load open positions and their protection from ``trading.positions``.

    One row per symbol is the whole truth: quantity, cost basis, leverage, the stop /
    target levels and the venue order ids. The newest order row is *not* a substitute:
    it carries no ``stop_order_id``, it may be missing between restarts, and reading a
    global window of orders left symbols with no protection at all — a monitor that
    only iterates ``ledger.protections`` would then not watch those positions.

    Realized PnL history cannot be reconstructed from a position row, so the loss
    windows restart at zero — a limitation recorded in the reconciliation runbook.
    """
    with session_scope(factory) as session:
        rows = (
            session.execute(select(PositionRecord).where(PositionRecord.qty != 0)).scalars().all()
        )

    positions = tuple(position_from_row(row) for row in rows)
    ledger.restore(positions)
    for position in positions:
        side = position.side
        if side is None or position.stop_loss is None:
            continue
        # The ids matter: without them a restart would re-arm a stop that is already
        # working, cancelling and re-placing it on every check.
        ledger.set_protection(
            PositionProtection(
                symbol=position.symbol,
                side=side,
                qty=position.abs_qty,
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
                stop_order_id=position.stop_order_id,
                tp_order_id=position.tp_order_id,
            )
        )
    return positions


def build_monitor(
    settings: Settings,
    factory: sessionmaker[Session],
    *,
    ledger: PortfolioLedger | None = None,
    writer: LedgerWriter | None = None,
    broker: Any = None,
    check_cadence: CheckLogCadence | None = None,
) -> PositionMonitor:
    """Wire a monitor from configuration (real gate, ledger writer, venue check).

    ``writer`` is what makes a close land in ``trading.trades`` and
    ``audit.audit_logs``: the caller passes a ``LedgerStore`` bound to the session
    that wraps one check, so an exit commits together with its audit trail.

    ``broker`` is shared with the venue check, so a caller that already holds one (the
    daemon builds it once) does not pay for a new HTTP client per check.
    """
    book = ledger or PortfolioLedger(initial_equity=settings.risk.initial_equity)
    if broker is None:
        broker = build_broker(
            settings.execution, exchange=settings.exchange, live_confirm=settings.live_confirm
        )
    venue_check = (
        BrokerVenueCheck(broker) if callable(getattr(broker, "fetch_positions", None)) else None
    )

    gateway = ExecutionGateway(broker, allow_live_orders=settings.execution.allow_live_orders)
    pipeline = TradingPipeline(
        gate=RiskGate.from_settings(settings),
        gateway=gateway,
        ledger=book,
        writer=writer,
    )
    return PositionMonitor(
        ledger=book,
        pipeline=pipeline,
        quotes=CandleQuoteSource.from_settings(settings, factory),
        use_live_price=settings.position_monitor.use_live_price,
        live_quotes=live_quote_source(settings, factory),
        protect_take_profit=settings.position_monitor.protect_take_profit,
        venue_check=venue_check,
        check_cadence=check_cadence,
    )


def action_payload(action: MonitorAction) -> dict[str, Any]:
    """JSON-ready summary of one exit attempt."""
    return {
        "symbol": action.symbol,
        "trigger": action.trigger.value,
        "level": str(action.level),
        "mark_price": str(action.mark_price),
        "approved": action.result.decision.approved,
        "filled": action.closed,
        "reasons": list(action.result.reasons),
        "trade_id": None if action.result.trade is None else action.result.trade.trade_id,
    }


def _check_once(monitor: PositionMonitor) -> tuple[MonitorAction, ...]:
    actions = monitor.check()
    print(json.dumps([action_payload(action) for action in actions], sort_keys=True))
    return actions


def book_venue_fills(
    settings: Settings,
    factory: sessionmaker[Session],
    ledger: PortfolioLedger,
    symbols: tuple[str, ...],
) -> None:
    """Reconcile after the venue closed a leg before the monitor could.

    Called *after* the check's write transaction has closed: the reconciler commits
    position rows and audit events in its own transaction. A failed pass is logged, not
    raised — withholding an exit is already the safe outcome, and the periodic
    reconciler retries.
    """
    if not symbols:
        return
    logger = get_logger("aegis.position_monitor")
    logger.warning("monitor_venue_execution_reconcile", symbols=list(symbols))
    try:
        reconcile_positions_once(settings, factory, ledger)
    except Exception as exc:  # a failed bookkeeping pass must not stop the monitor
        logger.error("monitor_venue_reconcile_failed", error=str(exc))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AegisTrade position monitor")
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    parser.add_argument("--interval", type=float, default=15.0, help="seconds between checks")
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="do not load positions from the database at startup",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)
    book = PortfolioLedger(initial_equity=settings.risk.initial_equity)
    # One broker for the whole run: its HTTP client is closed once, not once per check.
    broker = build_broker(
        settings.execution, exchange=settings.exchange, live_confirm=settings.live_confirm
    )

    try:
        if not args.no_restore:
            positions = restore_from_database(book, factory)
            print(
                f"position monitor: restored {len(positions)} open position(s)",
                file=sys.stderr,
            )

        # One cadence for the whole run: a fresh monitor per check would otherwise
        # restart the "still watching" timer and write a row every pass.
        cadence = CheckLogCadence()

        # One session per check: the closing order, its trade, the position upsert
        # and the audit rows commit together (see docs/runbooks/autonomous-trading.md).
        def _run_once() -> None:
            with session_scope(factory) as session:
                monitor = build_monitor(
                    settings,
                    factory,
                    ledger=book,
                    writer=LedgerStore(session),
                    broker=broker,
                    check_cadence=cadence,
                )
                _check_once(monitor)
            book_venue_fills(settings, factory, book, monitor.withheld_venue_executions)

        if args.once:
            _run_once()
        else:
            while True:
                _run_once()
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("position monitor stopped", file=sys.stderr)
    finally:
        close = getattr(broker, "close", None)
        if callable(close):
            close()
        engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
