"""Durable state of the ledger-vs-venue comparison.

``trading.reconciliation_state`` is a singleton row (``id = 1``). It is what makes
"entries stay off until a human looks" survive a restart: the runner reads it before
every entry cycle, and only an explicit acknowledgement clears it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from aegis_db import ReconciliationStateRecord, session_scope
from order_reconciler.positions import PositionDrift

__all__ = ["ReconciliationSnapshot", "SqlReconciliationState"]

_SINGLETON_ID = 1


@dataclass(frozen=True)
class ReconciliationSnapshot:
    """What the last pass found (``unchecked`` when nothing has run yet)."""

    status: str
    entries_halted: bool
    drift_count: int
    detail: tuple[dict[str, Any], ...]
    checked_at: datetime | None
    detected_at: datetime | None

    @property
    def blocked_reason(self) -> str | None:
        if not self.entries_halted:
            return None
        return f"ledger_drift:{self.drift_count}"

    @property
    def label(self) -> str:
        """Short text for the header badge."""
        if self.status == "synced":
            return "Ledger synced"
        if self.status == "drift":
            return "Ledger drift!"
        return "Ledger unchecked"


class SqlReconciliationState:
    """Reads and writes the singleton row (caller may pass a factory or a session)."""

    def __init__(self, source: sessionmaker[Session] | Session) -> None:
        self._source = source

    @contextmanager
    def _scope(self) -> Iterator[Session]:
        if isinstance(self._source, Session):
            yield self._source
            return
        with session_scope(self._source) as session:
            yield session

    def load(self) -> ReconciliationSnapshot:
        """Current state; an absent row means nothing has been checked yet."""
        with self._scope() as session:
            row = session.execute(
                select(ReconciliationStateRecord).where(
                    ReconciliationStateRecord.id == _SINGLETON_ID
                )
            ).scalar_one_or_none()
        if row is None:
            return ReconciliationSnapshot(
                status="unchecked",
                entries_halted=False,
                drift_count=0,
                detail=(),
                checked_at=None,
                detected_at=None,
            )
        return ReconciliationSnapshot(
            status=row.status,
            entries_halted=row.entries_halted,
            drift_count=row.drift_count,
            detail=tuple(row.detail or ()),
            checked_at=row.checked_at,
            detected_at=row.detected_at,
        )

    def record_synced(self, at: datetime) -> None:
        """Write "they agree"; a previously acknowledged drift is cleared."""
        self._upsert(
            {
                "status": "synced",
                "entries_halted": False,
                "drift_count": 0,
                "detail": None,
                "checked_at": at,
                "detected_at": None,
                "acknowledged_at": None,
            }
        )

    def block_entries(self, drifts: Sequence[PositionDrift], at: datetime) -> None:
        """Halt entries and keep the evidence of why."""
        self._upsert(
            {
                "status": "drift",
                "entries_halted": True,
                "drift_count": len(drifts),
                "detail": [drift.as_payload() for drift in drifts],
                "checked_at": at,
                "detected_at": at,
                "acknowledged_at": None,
            }
        )

    def acknowledge(self, at: datetime) -> None:
        """Operator says the book was fixed: resume entries on the next pass.

        The status becomes ``unchecked`` rather than ``synced`` on purpose: a human
        clearing the flag is not evidence that the venue agrees, and the next
        comparison will say so either way.
        """
        self._upsert(
            {
                "status": "unchecked",
                "entries_halted": False,
                "drift_count": 0,
                "detail": None,
                "checked_at": None,
                "detected_at": None,
                "acknowledged_at": at,
            }
        )

    def _upsert(self, values: dict[str, Any]) -> None:
        table = cast(Table, ReconciliationStateRecord.__table__)
        statement = (
            pg_insert(table)
            .values(id=_SINGLETON_ID, **values)
            .on_conflict_do_update(index_elements=["id"], set_=values)
        )
        with self._scope() as session:
            session.execute(statement)
