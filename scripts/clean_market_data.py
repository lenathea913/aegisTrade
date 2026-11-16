"""Delete every stored candle so the database starts from clean, venue-tagged data.

The collector used to write synthetic 2024 seed bars into ``market.candles`` before it
was wired to the live klines API, so a fresh look at the table mixed two years of fake
prices with real testnet bars. This clears the table and lets the collector refill it
from one venue.

Only ``market.candles`` is touched: orders, positions, trades and the audit log are the
record of what happened and are never rewritten by a data reset.

    python scripts/clean_market_data.py --yes
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import delete, func, select

from aegis_config import load_settings
from aegis_db import Candle as CandleRow
from aegis_db import create_engine_from_settings, create_session_factory, session_scope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete every row in market.candles")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (used by `make clean-db`)",
    )
    parser.add_argument(
        "--environment",
        choices=("testnet", "mainnet"),
        default=None,
        help="only clear one venue's rows instead of the whole table",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)

    try:
        with session_scope(factory) as session:
            statement = select(func.count()).select_from(CandleRow)
            if args.environment is not None:
                statement = statement.where(CandleRow.environment == args.environment)
            before = session.execute(statement).scalar_one()

            if before and not args.yes:
                scope = args.environment or "every environment"
                answer = input(f"Delete {before} candle(s) for {scope}? type 'yes': ")
                if answer.strip().lower() != "yes":
                    print("aborted", file=sys.stderr)
                    return 1

            remove = delete(CandleRow)
            if args.environment is not None:
                remove = remove.where(CandleRow.environment == args.environment)
            deleted = session.execute(remove).rowcount
    finally:
        engine.dispose()

    print(json.dumps({"deleted": deleted, "environment": args.environment or "all"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
