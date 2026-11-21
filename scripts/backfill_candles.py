"""Fill ``market.candles`` with history by paginating a Binance klines endpoint.

The collector only ever asks for the latest page, which is right for keeping a live
series current and useless for research: a one-year backtest needs ~8 760 hourly bars,
and no single request returns more than 1 000. This script walks *backwards* — each
page is anchored one millisecond before the oldest bar already received — and upserts
every page through the same idempotent store the collector uses, so re-running it is
free and can never duplicate a bar.

Default host is the **futures testnet** (``/fapi/v1/klines``): the series then matches
the USDT-M contract the backtest trades, and the spot testnet only keeps a few weeks
of history. ``--base-url``/``--path`` point it anywhere else that speaks the same
format.

Usage:
    python scripts/backfill_candles.py                      # 10 000 hourly bars, BTC/USDT
    python scripts/backfill_candles.py --target 12000
    python scripts/backfill_candles.py --symbol ETH/USDT --timeframe 4h
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from sqlalchemy.orm import Session

from aegis_config import load_settings
from aegis_core import Timeframe
from aegis_db import create_engine_from_settings, create_session_factory, session_scope
from market_collector.sources import BinanceKlineSource
from market_collector.store import CandleStore

FUTURES_TESTNET_BASE_URL = "https://testnet.binancefuture.com"
FUTURES_KLINES_PATH = "/fapi/v1/klines"
PAGE_LIMIT = 1000


def backfill(
    source: BinanceKlineSource,
    store: CandleStore,
    *,
    symbol: str,
    timeframe: Timeframe,
    target: int,
    environment: str,
    venue: str,
    exchange: str = "binance",
    page_limit: int = PAGE_LIMIT,
    after_page: object = None,
) -> tuple[int, int]:
    """Page backwards until ``target`` bars are seen; return (fetched, inserted).

    Stops early for any of three reasons, each of which means "the series ends here":
    a short page, an empty page, or a page that does not move the anchor backwards.
    Without that last guard a venue that ignores ``endTime`` would loop forever.
    """
    fetched = 0
    inserted = 0
    end_time = None
    max_pages = target // max(1, page_limit) + 5

    for page in range(1, max_pages + 1):
        candles = source.fetch(symbol, timeframe, page_limit, end_time=end_time)
        if not candles:
            break

        inserted += store.upsert(
            candles,
            exchange=exchange,
            source=source.label,
            environment=environment,
            venue=venue,
        )
        fetched += len(candles)
        oldest = candles[0].open_time

        if callable(after_page):
            after_page(page, len(candles), oldest, fetched, inserted)

        if fetched >= target:
            break
        # A short page only means "no more history" once we are walking backwards:
        # the first (latest) page legitimately loses the still-forming candle, so it
        # can be one row short without the series being finished.
        if len(candles) < page_limit and end_time is not None:
            break
        next_end = oldest - timedelta(milliseconds=1)
        if end_time is not None and next_end >= end_time:
            break
        end_time = next_end

    return fetched, inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument(
        "--timeframe",
        default=Timeframe.H1.value,
        choices=[timeframe.value for timeframe in Timeframe],
    )
    parser.add_argument("--target", type=int, default=10000, help="candles to collect")
    parser.add_argument("--page-limit", type=int, default=PAGE_LIMIT)
    parser.add_argument("--base-url", default=FUTURES_TESTNET_BASE_URL)
    parser.add_argument("--path", default=FUTURES_KLINES_PATH)
    parser.add_argument("--environment", default=None, help="defaults to the configured one")
    parser.add_argument(
        "--venue",
        choices=["spot", "futures"],
        default=None,
        help="market these bars belong to (default: futures for /fapi paths, else spot)",
    )
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    timeframe = Timeframe(args.timeframe)
    settings = load_settings()
    environment = args.environment or settings.exchange.environment
    # The endpoint decides the market unless the operator says otherwise: a /fapi path
    # is a perpetual contract, /api/v3 is spot. Labelling both "spot" is exactly how
    # the two series got mixed before ``venue`` existed.
    venue = args.venue or ("futures" if "fapi" in args.path else "spot")

    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)

    def report(page: int, page_size: int, oldest: object, fetched: int, inserted: int) -> None:
        print(
            f"page {page:>3}: {page_size:>4} bars, oldest {oldest}, "
            f"total {fetched} fetched / {inserted} new",
            file=sys.stderr,
        )

    with (
        BinanceKlineSource(
            base_url=args.base_url, klines_path=args.path, timeout_seconds=args.timeout
        ) as source,
        session_scope(factory) as session,
    ):
        assert isinstance(session, Session)
        fetched, inserted = backfill(
            source,
            CandleStore(session),
            symbol=args.symbol,
            timeframe=timeframe,
            target=args.target,
            environment=environment,
            venue=venue,
            exchange=args.exchange,
            page_limit=max(1, min(args.page_limit, 1000)),
            after_page=report,
        )

    engine.dispose()
    print(
        f"{args.symbol} {timeframe.value}: fetched {fetched}, inserted {inserted} "
        f"into {environment}/{venue} from {args.base_url}{args.path}"
    )
    if fetched < args.target:
        print(
            f"warning: only {fetched} bars were available (target {args.target}); "
            "the venue has no more history behind this point",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
