"""Verify AegisTrade against a **real** PostgreSQL instance.

Fakes cannot prove that SQLAlchemy's PostgreSQL dialect, the hand-written migration,
the append-only triggers, JSONB encoding or ``timestamptz`` round-trips actually work.
This script does, and exits non-zero on the first class of failure it finds.

It writes a few rows (one audit event, one order/trade/position from a paper fill and
the CSV seed candles) and cleans up everything it is allowed to delete. The audit row
cannot be removed — that is the point of the append-only trigger.

Usage:
    AEGIS_DATABASE__URL=postgresql+psycopg://user:pass@host:5432/db \\
        uv run python scripts/verify_postgres.py
    uv run python scripts/verify_postgres.py --skip-collector   # no candle ingest
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
from sqlalchemy import text

from aegis_config import load_settings
from aegis_core import MarketContext, MarketQuote, OrderIntent, OrderType, Side
from aegis_db import (
    OrderRecord,
    TradeRecord,
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from aegis_execution import ExecutionGateway, PaperBroker, TradingPipeline
from aegis_ledger import LedgerStore, PortfolioLedger
from aegis_risk import RiskGate

__all__ = ["main"]

FAILURES: list[str] = []
MOMENT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SYMBOL = "BTC/USDT"


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}{'' if condition else f' :: {detail}'}")
    if not condition:
        FAILURES.append(name)


def _raises(
    cursor: psycopg.Cursor[tuple[object, ...]], sql: str, params: tuple[object, ...] = ()
) -> str | None:
    """Run ``sql`` expecting a refusal; return the error text when it is refused."""
    try:
        cursor.execute(sql, params)
    except Exception as exc:
        cursor.connection.rollback()  # type: ignore[union-attr]
        return str(exc).splitlines()[0]
    cursor.connection.rollback()  # type: ignore[union-attr]
    return None


def _dsn(database_url: str) -> str:
    """SQLAlchemy URL -> psycopg DSN."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _schema_checks(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cursor = conn.cursor()
        cursor.execute("select version()")
        print(f"server: {cursor.fetchone()[0]}\n")

        cursor.execute(
            "select schema_name from information_schema.schemata "
            "where schema_name in ('market','trading','audit')"
        )
        check(
            "schemas market/trading/audit exist",
            sorted(row[0] for row in cursor.fetchall()) == ["audit", "market", "trading"],
        )

        cursor.execute("select version_num from alembic_version")
        print(f"alembic version   : {cursor.fetchone()[0]}")

        cursor.execute(
            "insert into audit.audit_logs (event_type, entity_type, entity_id, actor, payload) "
            "values ('order_approved', 'order', 'verify', 'verify', '{\"k\": 1}'::jsonb) "
            "returning id, created_at, payload"
        )
        row_id, created_at, payload = cursor.fetchone()
        check("JSONB payload round-trips", payload == {"k": 1}, str(payload))
        check("timestamptz is timezone-aware", created_at.tzinfo is not None, str(created_at))

        for statement, label, params in (
            ("update audit.audit_logs set actor = 'tampered' where id = %s", "UPDATE", (row_id,)),
            ("delete from audit.audit_logs where id = %s", "DELETE", (row_id,)),
            ("truncate audit.audit_logs", "TRUNCATE", ()),
        ):
            error = _raises(cursor, statement, params)
            check(
                f"{label} on audit.audit_logs is refused",
                error is not None and "append-only" in error.lower(),
                str(error),
            )

        candle_sql = (
            "insert into market.candles (exchange, environment, symbol, timeframe, open_time, "
            "close_time, open, high, low, close, volume, trades, source) values "
            "('binance', 'testnet', 'BTC/USDT', '1h', '2026-01-01T00:00:00+00', "
            "'2026-01-01T01:00:00+00', 100, 101, 99, 100.5, 10, 5, 'verify') "
            "on conflict (environment, symbol, timeframe, open_time) do nothing"
        )
        cursor.execute("delete from market.candles where source = 'verify'")
        cursor.execute(candle_sql)
        cursor.execute(candle_sql)
        cursor.execute("select count(*) from market.candles where source = 'verify'")
        check("ON CONFLICT DO NOTHING is idempotent", cursor.fetchone()[0] == 1)
        cursor.execute("delete from market.candles where source = 'verify'")

        error = _raises(
            cursor,
            "insert into trading.positions (symbol, qty, avg_entry) values ('BTC/USDT', 1, 0)",
        )
        check("positions CHECK rejects non-flat with zero basis", error is not None, str(error))

        # A flat row may not keep a stop: a restart would then watch an exit for
        # exposure that no longer exists.
        error = _raises(
            cursor,
            "insert into trading.positions (symbol, qty, avg_entry, stop_loss) "
            "values ('BTC/USDT', 0, 0, 29000)",
        )
        check("positions CHECK rejects a flat position with a stop", error is not None, str(error))

        cursor.execute(
            "insert into trading.reconciliation_state (id, status, entries_halted) "
            "values (1, 'drift', true) "
            "on conflict (id) do update set status = excluded.status, "
            "entries_halted = excluded.entries_halted"
        )
        cursor.execute(
            "select status, entries_halted from trading.reconciliation_state where id = 1"
        )
        check("reconciliation_state upserts its singleton", cursor.fetchone() == ("drift", True))
        cursor.execute(
            "update trading.reconciliation_state set status = 'unchecked', entries_halted = false"
        )


def _pipeline_check(settings: object) -> None:
    from aegis_config import Settings

    assert isinstance(settings, Settings)
    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)
    ledger = PortfolioLedger(initial_equity=settings.risk.initial_equity)

    try:
        with session_scope(factory) as session:
            pipeline = TradingPipeline(
                gate=RiskGate.from_settings(settings),
                gateway=ExecutionGateway(PaperBroker()),
                ledger=ledger,
                writer=LedgerStore(session),
            )
            intent = OrderIntent(
                symbol=SYMBOL,
                side=Side.BUY,
                order_type=OrderType.MARKET,
                qty=Decimal("0.03"),
                price=Decimal("30000"),
                stop_loss=Decimal("29000"),
                take_profit=Decimal("32000"),
                strategy_id="verify",
                reason="postgres verification fill",
                created_at=MOMENT,
            )
            market = MarketContext(
                quotes=(MarketQuote(symbol=SYMBOL, price=Decimal("30000"), as_of=MOMENT),),
                now=MOMENT,
            )
            result = pipeline.process(intent, market)
            check("paper fill approved and executed", result.filled is True, str(result.reasons))
            if result.execution is None or result.trade is None:
                return
            order_id = result.execution.order.order_id
            trade_id = result.trade.trade_id

        with session_scope(factory) as session:
            order = session.get(OrderRecord, order_id)
            trade = session.get(TradeRecord, trade_id)
            check("order row written", order is not None)
            check(
                "risk_checks JSONB round-trips",
                order is not None and bool(order.risk_checks),
                str(order.risk_checks if order else None),
            )
            check(
                "numeric money round-trips exactly",
                order is not None and order.qty == Decimal("0.03"),
                str(order.qty if order else None),
            )
            check(
                "timestamptz approved_at is aware",
                order is not None and order.approved_at.tzinfo is not None,
            )
            check("trade row written", trade is not None and trade.price > 0)
    finally:
        engine.dispose()


def _collector_check(settings: object) -> None:
    from aegis_config import Settings, load_settings
    from market_collector.cli import build_collector

    assert isinstance(settings, Settings)
    # This script verifies the database, not the venue: it pins the collector to the
    # committed CSV seeds so the check never depends on an external exchange being
    # reachable from CI (the default source is the live klines API).
    offline = load_settings(overrides={"market_data": {"source": "csv"}})
    engine = create_engine_from_settings(offline.database)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            session.execute(text("delete from market.candles where source != 'verify'"))
            first = build_collector(offline, session).run_once()
        with session_scope(factory) as session:
            second = build_collector(offline, session).run_once()
        print(f"collector run 1   : inserted={first.inserted} fetched={first.fetched}")
        print(f"collector run 2   : inserted={second.inserted} fetched={second.fetched}")
        check("first pass inserts candles", first.inserted > 0, str(first.inserted))
        check("second pass inserts nothing", second.inserted == 0, str(second.inserted))
        check("collector reports no failures", not first.failures and not second.failures)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="verify a real PostgreSQL deployment")
    parser.add_argument("--skip-collector", action="store_true", help="skip candle ingest")
    args = parser.parse_args(argv)

    settings = load_settings()
    dsn = _dsn(str(settings.database.url))
    print(f"target: {settings.database.url}\n")

    _schema_checks(dsn)
    _pipeline_check(settings)
    if not args.skip_collector and os.getenv("AEGIS_SKIP_COLLECTOR") != "1":
        _collector_check(settings)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL POSTGRES CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
