"""Position monitor command line.

    python -m position_monitor --once                 # one check, then exit
    python -m position_monitor --interval 15          # loop (SIGINT to stop)
    python -m position_monitor --once --no-restore    # ignore positions in the DB

The monitor runs in its own process, so it loads the open positions and their stop /
target levels from the database at startup (``--restore``, on by default). Without
that it would watch an empty ledger, because ledger state is not yet rebuilt from the
tables automatically (see ``docs/runbooks/ledger-reconciliation.md``).

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
    OrderRecord,
    PositionRecord,
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from aegis_execution import ExecutionGateway, TradingPipeline, build_broker
from aegis_ledger import LedgerStore, LedgerWriter, PortfolioLedger, PositionProtection
from aegis_risk import RiskGate
from position_monitor.monitor import MonitorAction, PositionMonitor
from position_monitor.quotes import CandleQuoteSource, live_quote_source

__all__ = ["action_payload", "build_monitor", "main", "restore_from_database"]


def restore_from_database(
    ledger: PortfolioLedger, factory: sessionmaker[Session]
) -> tuple[Position, ...]:
    """Load open positions and the newest stop/target per symbol from the tables.

    Realized PnL history cannot be reconstructed from a position row, so the loss
    windows restart at zero — a limitation recorded in the reconciliation runbook.
    """
    with session_scope(factory) as session:
        position_rows = (
            session.execute(select(PositionRecord).where(PositionRecord.qty != 0)).scalars().all()
        )
        order_rows = (
            session.execute(select(OrderRecord).order_by(OrderRecord.created_at.desc()).limit(200))
            .scalars()
            .all()
        )

    positions = tuple(
        Position(
            symbol=row.symbol,
            qty=row.qty,
            avg_entry=row.avg_entry,
            unrealized_pnl=row.unrealized_pnl,
            realized_pnl=row.realized_pnl,
            opened_at=row.opened_at,
        )
        for row in position_rows
    )
    ledger.restore(positions)

    newest: dict[str, OrderRecord] = {}
    for order in order_rows:
        newest.setdefault(order.symbol, order)

    for position in positions:
        latest_order = newest.get(position.symbol)
        side = position.side
        if latest_order is None or side is None:
            continue
        ledger.set_protection(
            PositionProtection(
                symbol=position.symbol,
                side=side,
                qty=position.abs_qty,
                stop_loss=latest_order.stop_loss,
                take_profit=latest_order.take_profit,
                strategy_id=latest_order.strategy_id,
            )
        )
    return positions


def build_monitor(
    settings: Settings,
    factory: sessionmaker[Session],
    *,
    ledger: PortfolioLedger | None = None,
    writer: LedgerWriter | None = None,
) -> PositionMonitor:
    """Wire a monitor from configuration (paper broker, real gate, ledger writer).

    ``writer`` is what makes a close land in ``trading.trades`` and
    ``audit.audit_logs``: the caller passes a ``LedgerStore`` bound to the session
    that wraps one check, so an exit commits together with its audit trail.
    """
    book = ledger or PortfolioLedger(initial_equity=settings.risk.initial_equity)
    broker = build_broker(
        settings.execution, exchange=settings.exchange, live_confirm=settings.live_confirm
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
        quotes=CandleQuoteSource(factory, environment=settings.exchange.environment),
        use_live_price=settings.position_monitor.use_live_price,
        live_quotes=live_quote_source(settings, factory),
        protect_take_profit=settings.position_monitor.protect_take_profit,
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

    try:
        if not args.no_restore:
            positions = restore_from_database(book, factory)
            print(
                f"position monitor: restored {len(positions)} open position(s)",
                file=sys.stderr,
            )

        # One session per check: the closing order, its trade, the position upsert
        # and the audit rows commit together (see docs/runbooks/autonomous-trading.md).
        def _run_once() -> None:
            with session_scope(factory) as session:
                monitor = build_monitor(settings, factory, ledger=book, writer=LedgerStore(session))
                _check_once(monitor)

        if args.once:
            _run_once()
        else:
            while True:
                _run_once()
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("position monitor stopped", file=sys.stderr)
    finally:
        engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
