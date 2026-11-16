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
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from aegis_config import Settings, load_settings
from aegis_db import create_engine_from_settings, create_session_factory, session_scope
from aegis_execution import ExchangeOrderState, build_broker
from aegis_ledger import LedgerError, LedgerStore, PortfolioLedger
from aegis_observability import configure_logging, get_logger
from order_reconciler.reconciler import OrderReconciler, ReconciliationResult
from order_reconciler.repository import SqlOrderBook

__all__ = ["build_reconciler", "main", "reconcile_once", "result_payload"]


class _BrokerFeed:
    """Adapts a broker to ``ExchangeOrderFeed`` (fetch only)."""

    def __init__(self, broker: Any) -> None:
        self._broker = broker

    def fetch(self, exchange_order_id: str, *, symbol: str) -> ExchangeOrderState:
        return cast(ExchangeOrderState, self._broker.fetch(exchange_order_id, symbol=symbol))


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


def _run_once(
    settings: Settings, factory: sessionmaker[Session], broker: Any, ledger: PortfolioLedger
) -> tuple[ReconciliationResult, ...]:
    with session_scope(factory) as session:
        return build_reconciler(settings, session, broker, ledger).run()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AegisTrade order reconciler")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.observability)
    logger = get_logger("aegis.order_reconciler")

    if settings.execution.broker == "paper":
        logger.warning(
            "reconciler_skipped",
            detail="paper broker fills synchronously; there is nothing to reconcile",
        )
        return 0

    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)
    ledger = PortfolioLedger(initial_equity=settings.risk.initial_equity)
    _restore_ledger(ledger, factory)
    logger.info(
        "reconciler_started",
        broker=settings.execution.broker,
        interval_seconds=settings.reconciler.interval_seconds,
    )
    try:
        while True:
            results = reconcile_once(settings, factory, ledger)
            changed = [result for result in results if result.action != "unchanged"]
            if results:
                print(
                    json.dumps([result_payload(item) for item in changed], sort_keys=True),
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
