"""Enumerations shared across the domain and services."""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    """Order side."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL (used for signed position deltas)."""
        return 1 if self is Side.BUY else -1


class OrderType(StrEnum):
    """Supported order types. Anything exotic must be added deliberately."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class Timeframe(StrEnum):
    """Supported candle intervals.

    Kept deliberately small and closed: the collector's idempotency key and the
    database CHECK constraint both derive from this list.
    """

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def duration_seconds(self) -> int:
        """Bar length in seconds; the basis of the derived freshness window."""
        return _TIMEFRAME_SECONDS[self]


#: Bar length per timeframe. Kept next to the enum so a new timeframe cannot be added
#: without deciding how long one of its bars lasts.
_TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
}


class OrderStatus(StrEnum):
    """Lifecycle of a submitted order."""

    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)
