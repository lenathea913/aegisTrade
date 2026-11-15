"""UTC clock helpers.

Every timestamp in AegisTrade is timezone-aware UTC and stored as ``timestamptz``.
Naive datetimes are rejected at the domain boundary; non-UTC aware datetimes are
converted (never silently dropped) so two events can always be ordered.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(tz=UTC)


def to_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC.

    Raises:
        ValueError: if ``value`` is naive (no timezone information).
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime is not allowed; all times must be timezone-aware UTC")
    return value.astimezone(UTC)
