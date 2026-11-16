"""Mark parsing, the mark store and the reconnect loop's URL shape."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aegis_core import MarketQuote
from market_streamer import (
    BinanceMarkStream,
    InMemoryMarkStore,
    Mark,
    RedisMarkStore,
    fresh,
    from_stream_symbol,
    mark_from_payload,
)
from position_monitor.quotes import MarkStoreQuoteSource

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def book_ticker(
    symbol: str = "BTCUSDT", bid: str = "29999", ask: str = "30001"
) -> dict[str, object]:
    return {"stream": f"{symbol.lower()}@bookTicker", "data": {"s": symbol, "b": bid, "a": ask}}


def test_mid_price_and_symbol_are_extracted() -> None:
    mark = mark_from_payload(book_ticker(), NOW)

    assert mark is not None
    assert mark.symbol == "BTC/USDT"
    assert mark.price == Decimal("30000")
    assert mark.observed_at == NOW


def test_unknown_quote_asset_is_ignored() -> None:
    assert mark_from_payload({"data": {"s": "BTCXYZ", "b": "1", "a": "2"}}) is None
    assert from_stream_symbol("BTCUSDT") == "BTC/USDT"


def test_garbage_never_raises() -> None:
    assert mark_from_payload({"data": {"s": "BTCUSDT", "b": None}}) is None
    assert mark_from_payload({"data": "nope"}) is None


def test_stream_handles_text_and_bytes_and_stores_the_mark() -> None:
    store = InMemoryMarkStore()
    stream = BinanceMarkStream(
        url="wss://stream.testnet.binance.vision/stream",
        symbols=("BTC/USDT",),
        store=store,
        clock=lambda: NOW,
    )

    assert stream.handle(b'{"data": {"s": "BTCUSDT", "b": "100", "a": "102"}}') is not None
    assert stream.handle("not json") is None
    assert store.latest("BTC/USDT") == Mark("BTC/USDT", Decimal("101"), NOW)


def test_stream_url_combines_every_symbol() -> None:
    stream = BinanceMarkStream(
        url="wss://stream.testnet.binance.vision/stream",
        symbols=("BTC/USDT", "ETH/USDT"),
        store=InMemoryMarkStore(),
    )

    assert stream.stream_url.endswith("streams=btcusdt@bookTicker/ethusdt@bookTicker")


def test_stream_reconnects_after_a_transport_error() -> None:
    store = InMemoryMarkStore()
    attempts: list[str] = []
    stop = threading.Event()

    class Boom:
        def __enter__(self) -> Boom:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def __iter__(self) -> Iterator[str]:
            raise ConnectionError("socket died")

    def connect(url: str) -> Boom:
        attempts.append(url)
        if len(attempts) >= 2:
            # The second failure is enough: prove the loop retried, then stop it.
            stop.set()
        return Boom()

    stream = BinanceMarkStream(
        url="wss://example/stream",
        symbols=("BTC/USDT",),
        store=store,
        reconnect_seconds=0.01,
        connect=connect,
    )

    stream.run(stop)

    assert len(attempts) == 2
    assert store.latest("BTC/USDT") is None


def test_redis_store_round_trips_through_a_fake_client() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}
            self.ttls: dict[str, int] = {}

        def set(self, key: str, value: str, ex: int | None = None) -> None:
            self.data[key] = value
            self.ttls[key] = ex or 0

        def get(self, key: str) -> str | None:
            return self.data.get(key)

        def ping(self) -> bool:
            return True

    client = FakeRedis()
    store = RedisMarkStore(client, ttl_seconds=15)

    store.set(Mark("BTC/USDT", Decimal("30000"), NOW))

    assert client.ttls["aegis:mark:BTC/USDT"] == 15
    assert store.latest("BTC/USDT") == Mark("BTC/USDT", Decimal("30000"), NOW)
    assert store.latest("ETH/USDT") is None


def test_stale_marks_are_not_fresh() -> None:
    mark = Mark("BTC/USDT", Decimal("30000"), NOW - timedelta(seconds=30))

    assert fresh(mark, max_age_seconds=15, now=NOW) is False
    assert fresh(Mark("BTC/USDT", Decimal("30000"), NOW), max_age_seconds=15, now=NOW) is True


def test_live_quote_source_prefers_fresh_marks_and_falls_back() -> None:
    store = InMemoryMarkStore()
    store.set(Mark("BTC/USDT", Decimal("30000"), NOW - timedelta(seconds=1)))
    store.set(Mark("ETH/USDT", Decimal("2000"), NOW - timedelta(seconds=60)))
    fallback = _FixedQuotes(
        {"ETH/USDT": MarketQuote(symbol="ETH/USDT", price=Decimal("1900"), as_of=NOW)}
    )
    source = MarkStoreQuoteSource(store, max_age_seconds=15, fallback=fallback, clock=lambda: NOW)

    fresh_quote = source.latest("BTC/USDT")
    stale_quote = source.latest("ETH/USDT")
    missing = source.latest("SOL/USDT")

    assert fresh_quote is not None and fresh_quote.price == Decimal("30000")
    assert stale_quote is not None and stale_quote.price == Decimal("1900")
    assert missing is None


class _FixedQuotes:
    def __init__(self, quotes: dict[str, MarketQuote]) -> None:
        self._quotes = quotes

    def latest(self, symbol: str) -> MarketQuote | None:
        return self._quotes.get(symbol)


@pytest.mark.parametrize("symbol", ["BTC/USDT", "ETH/USDT"])
def test_every_configured_symbol_gets_a_stream(symbol: str) -> None:
    stream = BinanceMarkStream(
        url="wss://example/stream", symbols=(symbol,), store=InMemoryMarkStore()
    )

    assert symbol.replace("/", "").lower() in stream.stream_url
