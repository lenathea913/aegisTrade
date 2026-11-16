"""Collector command line.

    python -m market_collector --once                # one pass, then exit
    python -m market_collector --interval 60         # loop (SIGINT to stop)

Paper/dev mode reads committed CSV seeds, so no network and no credentials are
needed. ``source: exchange`` switches to public Binance klines.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from aegis_config import Settings, load_settings
from aegis_db import create_engine_from_settings, create_session_factory, session_scope
from aegis_observability import configure_logging, get_logger
from market_collector.collector import CollectionSummary, MarketCollector
from market_collector.retry import RetryPolicy
from market_collector.sources import (
    BINANCE_MAINNET_BASE_URL,
    BINANCE_TESTNET_BASE_URL,
    CandleSource,
    build_source,
)
from market_collector.store import CandleStore

__all__ = ["build_collector", "build_source_for", "klines_base_url", "main", "summary_payload"]


def klines_base_url(settings: Settings) -> str:
    """Where the klines request goes: the testnet host only when testnet is configured."""
    configured = settings.exchange.base_url.rstrip("/")
    if settings.exchange.testnet:
        return configured if "testnet.binance.vision" in configured else BINANCE_TESTNET_BASE_URL
    if "testnet.binance.vision" in configured:
        # ``exchange.base_url`` defaults to testnet while ``testnet: false`` asks for
        # mainnet prices; prefer the configured intent over the leftover default.
        return BINANCE_MAINNET_BASE_URL
    return configured


def build_source_for(settings: Settings) -> CandleSource:
    return build_source(
        settings.market_data.source,
        csv_dir=settings.market_data.csv_dir,
        timeout_seconds=settings.market_data.request_timeout_seconds,
        base_url=klines_base_url(settings),
    )


def build_collector(
    settings: Settings, session: Session, *, limit: int | None = None
) -> MarketCollector:
    retry = RetryPolicy(
        attempts=max(1, settings.market_data.max_retries),
        initial_delay=1.0,
        multiplier=2.0,
        max_delay=60.0,
    )
    return MarketCollector(
        source=build_source_for(settings),
        store=CandleStore(session),
        symbols=settings.market_data.symbols,
        timeframes=settings.market_data.timeframes,
        exchange=settings.market_data.exchange,
        environment=settings.exchange.environment,
        limit=limit or 500,
        retry=retry,
    )


def summary_payload(summary: CollectionSummary) -> dict[str, Any]:
    return {
        "started_at": summary.started_at.isoformat(),
        "fetched": summary.fetched,
        "inserted": summary.inserted,
        "max_lag_seconds": summary.max_lag_seconds,
        "series": [
            {
                "symbol": result.symbol,
                "timeframe": result.timeframe.value,
                "fetched": result.fetched,
                "inserted": result.inserted,
                "lag_seconds": result.lag_seconds,
                "error": result.error,
            }
            for result in summary.series
        ],
    }


def _run_once(
    settings: Settings, factory: sessionmaker[Session], limit: int | None
) -> CollectionSummary:
    with session_scope(factory) as session:
        collector = build_collector(settings, session, limit=limit)
        summary = collector.run_once()

    print(json.dumps(summary_payload(summary), sort_keys=True))
    for failure in summary.failures:
        print(
            f"collector: {failure.symbol} {failure.timeframe.value} failed: {failure.error}",
            file=sys.stderr,
        )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AegisTrade market data collector")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between passes")
    parser.add_argument("--limit", type=int, default=None, help="candles per request")
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.observability)
    logger = get_logger("aegis.collector")
    # Say which source this process uses, so a mis-wired env var is visible in the log
    # instead of being inferred from stale candles hours later.
    logger.info(
        "collector_started",
        source=settings.market_data.source,
        source_class=type(build_source_for(settings)).__name__,
        klines_base_url=klines_base_url(settings),
        testnet=settings.exchange.testnet,
        environment=settings.exchange.environment,
        symbols=list(settings.market_data.symbols),
    )
    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)

    try:
        if args.once:
            _run_once(settings, factory, args.limit)
        else:
            while True:
                _run_once(settings, factory, args.limit)
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("collector stopped", file=sys.stderr)
    finally:
        engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
