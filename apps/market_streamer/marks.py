"""Where the latest ticker price lives.

The streamer writes, the monitor reads; nothing else touches these keys. Redis is the
shared store because the two processes are separate containers, but an in-memory store
implements the same protocol so unit tests and a single-process demo need no server.

A mark carries the time it was observed. Consumers must check that age: a mark that
nobody refreshed is worse than no mark at all, because it looks actionable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from aegis_core import utc_now

__all__ = ["InMemoryMarkStore", "Mark", "MarkStore", "RedisMarkStore"]


@dataclass(frozen=True)
class Mark:
    """One observed price for one symbol."""

    symbol: str
    price: Decimal
    observed_at: datetime


class MarkStore(Protocol):
    """The write/read surface the streamer and the monitor need."""

    def set(self, mark: Mark) -> None:
        """Store the newest mark for ``mark.symbol``."""

    def latest(self, symbol: str) -> Mark | None:
        """Newest mark for ``symbol``, or ``None`` when nothing was ever written."""


class InMemoryMarkStore:
    """Process-local store; used by tests and by a streamer without Redis."""

    def __init__(self) -> None:
        self._marks: dict[str, Mark] = {}

    def set(self, mark: Mark) -> None:
        self._marks[mark.symbol] = mark

    def latest(self, symbol: str) -> Mark | None:
        return self._marks.get(symbol)


class RedisMarkStore:
    """Redis-backed store: one string key per symbol with a TTL.

    The TTL is the expiry of the *data*, not of the key's usefulness: a streamer that
    dies leaves marks that vanish instead of freezing the dashboard on a stale price.
    """

    def __init__(self, client: Any, *, ttl_seconds: int = 15, prefix: str = "aegis:mark") -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._prefix = prefix

    @classmethod
    def from_url(
        cls, url: str, *, ttl_seconds: int = 15, prefix: str = "aegis:mark"
    ) -> RedisMarkStore:
        """Build from a ``redis://`` URL (the client connects lazily)."""
        from redis import Redis

        return cls(Redis.from_url(url, socket_timeout=2.0), ttl_seconds=ttl_seconds, prefix=prefix)

    def key(self, symbol: str) -> str:
        return f"{self._prefix}:{symbol}"

    def set(self, mark: Mark) -> None:
        payload = json.dumps(
            {"price": str(mark.price), "observed_at": mark.observed_at.isoformat()}
        )
        self._client.set(self.key(mark.symbol), payload, ex=self._ttl)

    def latest(self, symbol: str) -> Mark | None:
        raw = self._client.get(self.key(symbol))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
            return Mark(
                symbol=symbol,
                price=Decimal(str(payload["price"])),
                observed_at=datetime.fromisoformat(str(payload["observed_at"])),
            )
        except (ValueError, KeyError, TypeError):
            # A corrupted cache entry is not worth crashing a stop-loss check over.
            return None

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False


def fresh(mark: Mark | None, *, max_age_seconds: float, now: datetime | None = None) -> bool:
    """Whether ``mark`` is recent enough to act on."""
    if mark is None:
        return False
    moment = now if now is not None else utc_now()
    return (moment - mark.observed_at).total_seconds() <= max_age_seconds
