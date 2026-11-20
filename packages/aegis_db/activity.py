"""Read and write the activity feed (``trading.bot_activity``).

Three properties shape this module:

* **Trading never depends on it.** A feed row is written in its own short transaction,
  after the trading one has committed, and a failure is logged instead of raised: an
  operator-facing message must never roll back or block an order.
* **Bounded by construction.** Every write has a retention policy behind it — the
  newest ``keep`` rows and nothing older than ``max_age`` — so the radar stays cheap
  without a cron job.
* **Injectable.** Callers take an ``ActivityWriter``; tests pass ``NullActivityWriter``
  or a list recorder and never need a database.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from aegis_db.models.activity import ACTIVITY_LEVELS, BotActivity
from aegis_db.session import session_scope
from aegis_observability import get_logger

__all__ = [
    "DEFAULT_KEEP_ROWS",
    "DEFAULT_MAX_AGE",
    "ActivityReader",
    "ActivityRecord",
    "ActivityWriter",
    "NullActivityWriter",
    "RecordingActivityWriter",
    "SqlActivityReader",
    "SqlActivityWriter",
    "prune_activity",
]

#: How many rows survive regardless of age (~16 hours of a 1-minute heartbeat).
DEFAULT_KEEP_ROWS = 1000
#: How old a row may be before it is dropped.
DEFAULT_MAX_AGE = timedelta(hours=24)
#: Pruning on every write would delete-scan continuously; once every five minutes is
#: invisible to an operator and costs one small query.
PRUNE_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True)
class ActivityRecord:
    """One row of the feed, in the shape the API and the template need."""

    id: int
    at: datetime
    event_type: str
    level: str
    message: str


class ActivityWriter(Protocol):
    """Anything that can append one line to the feed."""

    def record(self, event_type: str, message: str, *, level: str = "INFO") -> None:
        """Store ``message`` under ``event_type``; never raise into the caller."""


class NullActivityWriter:
    """Feed disabled (tests, dry runs): nothing is stored and nothing is logged."""

    def record(self, event_type: str, message: str, *, level: str = "INFO") -> None:
        return None


class RecordingActivityWriter:
    """In-memory writer: the test double that also documents the expected shape."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def record(self, event_type: str, message: str, *, level: str = "INFO") -> None:
        self.events.append((event_type, level, message))

    @property
    def messages(self) -> list[str]:
        return [message for _, _, message in self.events]


class SqlActivityWriter:
    """Appends rows with the retention policy applied on a timer."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        keep: int = DEFAULT_KEEP_ROWS,
        max_age: timedelta = DEFAULT_MAX_AGE,
        prune_interval_seconds: float = PRUNE_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if keep < 1:
            raise ValueError("keep must be at least 1")
        self._factory = factory
        self._keep = keep
        self._max_age = max_age
        self._prune_interval = prune_interval_seconds
        self._clock = clock
        self._last_prune: float | None = None
        self._logger = get_logger("aegis.activity")

    def record(self, event_type: str, message: str, *, level: str = "INFO") -> None:
        if level not in ACTIVITY_LEVELS:
            raise ValueError(f"unknown activity level: {level!r}")
        try:
            with session_scope(self._factory) as session:
                session.add(BotActivity(event_type=event_type, message=message, level=level))
        except SQLAlchemyError as exc:
            # Telemetry is best effort: the bot keeps trading without its radar.
            self._logger.warning("activity_write_failed", error=str(exc), event_type=event_type)
            return
        self._prune_if_due()

    def _prune_if_due(self) -> None:
        moment = self._clock()
        if self._last_prune is not None and moment - self._last_prune < self._prune_interval:
            return
        self._last_prune = moment
        try:
            prune_activity(self._factory, keep=self._keep, max_age=self._max_age)
        except SQLAlchemyError as exc:
            self._logger.warning("activity_prune_failed", error=str(exc))


def prune_activity(
    factory: sessionmaker[Session],
    *,
    keep: int = DEFAULT_KEEP_ROWS,
    max_age: timedelta = DEFAULT_MAX_AGE,
    now: datetime | None = None,
) -> int:
    """Delete aged rows and anything past the newest ``keep``; return rows removed.

    Two independent rules, because either one alone fails: age bounds a quiet bot's
    history, while the row cap bounds a chatty one (a heartbeat every minute never
    reaches 24 hours, a flood of rejections reaches the cap in minutes).
    """
    import datetime as _dt

    cutoff = (now or _dt.datetime.now(_dt.UTC)) - max_age
    with session_scope(factory) as session:
        expired = cast(
            CursorResult[Any],
            session.execute(delete(BotActivity).where(BotActivity.created_at < cutoff)),
        ).rowcount

        # The id of the first row that must survive; everything at or below it goes.
        # ``OFFSET keep`` is index-backed by the primary key, so this stays cheap.
        boundary = session.execute(
            select(BotActivity.id).order_by(BotActivity.id.desc()).offset(keep).limit(1)
        ).scalar_one_or_none()
        trimmed = 0
        if boundary is not None:
            trimmed = cast(
                CursorResult[Any],
                session.execute(delete(BotActivity).where(BotActivity.id <= boundary)),
            ).rowcount
    return int(expired or 0) + int(trimmed or 0)


class ActivityReader(Protocol):
    """The read surface the API needs."""

    def recent(self, limit: int) -> Sequence[ActivityRecord]:
        """Most recent activity, newest first."""


class SqlActivityReader:
    """PostgreSQL implementation; one short session per poll."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def recent(self, limit: int) -> Sequence[ActivityRecord]:
        with session_scope(self._factory) as session:
            rows = (
                session.execute(select(BotActivity).order_by(BotActivity.id.desc()).limit(limit))
                .scalars()
                .all()
            )
            return [
                ActivityRecord(
                    id=row.id,
                    at=row.created_at,
                    event_type=row.event_type,
                    level=row.level,
                    message=row.message,
                )
                for row in rows
            ]
