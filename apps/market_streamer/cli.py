"""Market streamer command line.

    python -m market_streamer

Reads ``exchange.testnet`` to decide which WebSocket endpoint to use, writes marks into
Redis when it is reachable and falls back to an in-memory store otherwise (the monitor
then simply has nothing shared to read, which it handles).
"""

from __future__ import annotations

import argparse
import signal
import threading
from collections.abc import Sequence
from typing import Any

from aegis_config import Settings, load_settings
from aegis_observability import configure_logging, get_logger
from market_streamer.marks import InMemoryMarkStore, MarkStore, RedisMarkStore
from market_streamer.stream import BinanceMarkStream

__all__ = ["build_store", "main", "stream_url"]


def stream_url(settings: Settings) -> str:
    """Testnet feed by default; mainnet only when the exchange section says so."""
    return (
        settings.streaming.testnet_url
        if settings.exchange.testnet
        else settings.streaming.mainnet_url
    )


def build_store(settings: Settings) -> MarkStore:
    """Redis when it answers, memory otherwise (logged, never fatal)."""
    logger = get_logger("aegis.market_streamer")
    try:
        store = RedisMarkStore.from_url(
            settings.redis.url, ttl_seconds=settings.streaming.mark_ttl_seconds
        )
        if store.ping():
            logger.info("mark_store_redis", url=settings.redis.url)
            return store
        logger.warning("mark_store_redis_unreachable", fallback="memory")
    except Exception as exc:
        logger.warning("mark_store_redis_failed", error=str(exc), fallback="memory")
    return InMemoryMarkStore()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AegisTrade real-time price streamer")
    parser.add_argument(
        "--once", action="store_true", help="read a single burst, print the URL and exit"
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.observability)
    logger = get_logger("aegis.market_streamer")

    if not settings.streaming.enabled:
        logger.warning(
            "market_streamer_disabled",
            detail="AEGIS_STREAMING__ENABLED is false; nothing was streamed",
        )
        return 0

    store = build_store(settings)
    stream = BinanceMarkStream(
        url=stream_url(settings),
        symbols=settings.market_data.symbols,
        store=store,
        reconnect_seconds=settings.streaming.reconnect_seconds,
    )

    if args.once:
        print(stream.stream_url)
        return 0

    stop = threading.Event()

    def _stop(signum: int, _frame: Any) -> None:
        logger.info("mark_streamer_stopping", signal=signum)
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info(
        "market_streamer_started",
        url=stream.stream_url,
        symbols=list(settings.market_data.symbols),
        testnet=settings.exchange.testnet,
    )
    try:
        stream.run(stop)
    finally:
        logger.info("market_streamer_stopped")
    return 0
