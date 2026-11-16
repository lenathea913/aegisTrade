"""Archive the paper-trading state to JSON, then empty the tables it came from.

A paper run is an experiment: sometimes the interesting thing is the CSV of trades, the
orders and the paper positions, and the next run should start from a flat book. This
script writes all of it to one timestamped JSON file first, and only then deletes.

What it archives and clears:

* ``trading.trades``, ``trading.orders``, ``trading.positions``
* ``trading.signals``
* ``market.candles``

What it never touches: ``audit.audit_logs``. That table is append-only and is the
record of *why* something happened; a reset is not a reason to lose it. Orders are
removed after trades because trades reference them.

    python scripts/paper_reset.py --yes
    python scripts/paper_reset.py --yes --output artifacts/reset-1.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from aegis_config import load_settings
from aegis_db import (
    Candle,
    OrderRecord,
    PositionRecord,
    SignalRecord,
    TradeRecord,
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)

#: Deletion order: children before the rows they reference.
TRADING_TABLES: tuple[Any, ...] = (TradeRecord, OrderRecord, PositionRecord, SignalRecord)
DEFAULT_OUTPUT_DIR = Path("artifacts") / "paper-reset"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _dump_rows(session: Session, model: Any) -> list[dict[str, Any]]:
    rows = session.execute(select(model)).scalars().all()
    return [
        {column.key: _jsonable(getattr(row, column.key)) for column in model.__table__.columns}
        for row in rows
    ]


def _count(session: Session, model: Any) -> int:
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def snapshot(session: Session) -> dict[str, list[dict[str, Any]]]:
    """Every row this reset would remove, as JSON-ready dicts."""
    payload = {model.__tablename__: _dump_rows(session, model) for model in TRADING_TABLES}
    payload["candles"] = _dump_rows(session, Candle)
    return payload


def _clear(session: Session, models: Iterable[Any]) -> dict[str, int]:
    cleared: dict[str, int] = {}
    for model in models:
        cleared[model.__tablename__] = int(session.execute(delete(model)).rowcount)
    return cleared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Archive trading.* and market.candles to JSON, then empty them"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (used by `make paper-reset`)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"archive path (default: {DEFAULT_OUTPUT_DIR}/<timestamp>.json)",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or DEFAULT_OUTPUT_DIR / f"paper-reset-{stamp}.json"

    try:
        with session_scope(factory) as session:
            counts = {model.__tablename__: _count(session, model) for model in TRADING_TABLES}
            counts["candles"] = _count(session, Candle)
            total = sum(counts.values())

            if total and not args.yes:
                summary = ", ".join(f"{name}={count}" for name, count in counts.items())
                answer = input(f"Archive and delete {total} row(s) ({summary})? type 'yes': ")
                if answer.strip().lower() != "yes":
                    print("aborted", file=sys.stderr)
                    return 1

            # Archive before deleting: a failed write leaves the database untouched.
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(
                    {
                        "created_at": datetime.now(UTC).isoformat(),
                        "environment": settings.environment.value,
                        "counts": counts,
                        "tables": snapshot(session),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            deleted = _clear(session, (*TRADING_TABLES, Candle))
    finally:
        engine.dispose()

    print(json.dumps({"archive": str(output), "deleted": deleted}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
