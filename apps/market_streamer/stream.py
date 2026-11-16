"""Binance public WebSocket feed -> mark prices.

Only public market data is used here: no API key, no signature, no order. The feed is
Binance's ``<symbol>@bookTicker`` stream, and the mark is the mid price of the best bid
and ask — the same quantity the monitor compares with a stop or target.

The URL follows ``exchange.testnet``: a testnet run reads testnet prices, so a paper
book is never valued with mainnet ticks.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from decimal import Decimal
from typing import Any

from aegis_core import utc_now
from aegis_observability import get_logger
from market_streamer.marks import Mark, MarkStore

__all__ = [
    "BinanceMarkStream",
    "from_stream_symbol",
    "mark_from_payload",
    "mid_price",
    "to_stream_symbol",
]

_QUOTE_ASSETS = ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH")
_TWO = Decimal("2")

#: ``connect(url)`` must return a context manager yielding an iterable of messages;
#: tests inject a fake, production uses ``websockets.sync.client.connect``.
ConnectionFactory = Callable[[str], AbstractContextManager[Any]]


def to_stream_symbol(symbol: str) -> str:
    """``BTC/USDT`` -> ``btcusdt`` (the stream name Binance expects)."""
    return symbol.replace("/", "").lower()


def from_stream_symbol(raw: str) -> str | None:
    """``BTCUSDT`` -> ``BTC/USDT``; ``None`` for a symbol without a known quote."""
    upper = raw.upper()
    for quote in _QUOTE_ASSETS:
        if upper.endswith(quote) and len(upper) > len(quote):
            return f"{upper[: -len(quote)]}/{quote}"
    return None


def mid_price(payload: dict[str, Any]) -> Decimal | None:
    """Mid price of the best bid/ask in a ``bookTicker`` payload."""
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    bid, ask = data.get("b"), data.get("a")
    if bid is None or ask is None:
        return None
    try:
        return (Decimal(str(bid)) + Decimal(str(ask))) / _TWO
    except (ArithmeticError, ValueError):
        return None


def mark_from_payload(payload: dict[str, Any], now: datetime | None = None) -> Mark | None:
    """Map one stream message to a :class:`Mark`, or ``None`` when it is not one."""
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    raw_symbol = data.get("s")
    if not isinstance(raw_symbol, str):
        return None
    symbol = from_stream_symbol(raw_symbol)
    price = mid_price(payload)
    if symbol is None or price is None or price <= 0:
        return None
    return Mark(symbol=symbol, price=price, observed_at=now if now is not None else utc_now())


def _default_connect(url: str) -> AbstractContextManager[Any]:
    from websockets.sync.client import connect

    return connect(url, open_timeout=10, ping_interval=20)


class BinanceMarkStream:
    """Reconnecting reader that writes every mark into a :class:`MarkStore`."""

    def __init__(
        self,
        *,
        url: str,
        symbols: tuple[str, ...] | list[str],
        store: MarkStore,
        reconnect_seconds: float = 5.0,
        connect: ConnectionFactory | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not symbols:
            raise ValueError("the mark stream needs at least one symbol")
        self._url = url
        self._symbols = tuple(symbols)
        self._store = store
        self._reconnect = reconnect_seconds
        self._connect: ConnectionFactory = connect or _default_connect
        self._clock = clock
        self._logger = get_logger("aegis.market_streamer")

    @property
    def stream_url(self) -> str:
        """Combined-stream URL for every configured symbol."""
        streams = "/".join(f"{to_stream_symbol(symbol)}@bookTicker" for symbol in self._symbols)
        separator = "&" if "?" in self._url else "?"
        return f"{self._url}{separator}streams={streams}"

    def handle(self, raw: str | bytes, *, now: datetime | None = None) -> Mark | None:
        """Parse one message and store it; returns the mark when it was usable."""
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        try:
            payload = json.loads(text)
        except ValueError:
            self._logger.warning("mark_stream_bad_payload", size=len(text))
            return None
        if not isinstance(payload, dict):
            return None
        mark = mark_from_payload(payload, now if now is not None else self._clock())
        if mark is None:
            return None
        self._store.set(mark)
        return mark

    def run(self, stop: threading.Event | None = None) -> None:
        """Read until ``stop`` is set, reconnecting with a fixed delay on failure."""
        while stop is None or not stop.is_set():
            try:
                with self._connect(self.stream_url) as connection:
                    self._logger.info(
                        "mark_stream_connected",
                        url=self.stream_url,
                        symbols=list(self._symbols),
                    )
                    for raw in connection:
                        if stop is not None and stop.is_set():
                            break
                        self.handle(raw)
            except Exception as exc:
                self._logger.warning("mark_stream_disconnected", error=str(exc))
            if stop is not None and stop.is_set():
                break
            time.sleep(self._reconnect)
