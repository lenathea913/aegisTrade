"""Append-only audit events and the journal the ledger writes them to.

Every important step (risk rejection, approval, submission, fill, trade, position
change) becomes one ``AuditEvent``. Events are immutable value objects; the journal
only ever appends, mirroring the ``audit.audit_logs`` table whose trigger refuses
``UPDATE``/``DELETE``.

Payload values are strings/numbers on purpose: a payload must survive JSONB and be
readable years later, so ``Decimal`` and ``datetime`` are stringified at the edge.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field

from aegis_core import StrictModel, utc_now
from aegis_core.types import NonEmptyStr, UtcDatetime

__all__ = ["AuditEvent", "EventType", "InMemoryJournal", "Journal"]


class EventType(StrEnum):
    """Stable audit event names. Renaming one breaks dashboards and queries."""

    RISK_REJECTED = "risk_rejected"
    ORDER_APPROVED = "order_approved"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_NOT_FILLED = "order_not_filled"
    TRADE_RECORDED = "trade_recorded"
    POSITION_UPDATED = "position_updated"
    MANUAL_CLOSE = "manual_close"
    PROTECTION_PLACED = "protection_placed"
    PROTECTION_FAILED = "protection_failed"
    POSITION_DRIFT_DETECTED = "position_drift_detected"
    POSITION_DRIFT_ACKNOWLEDGED = "position_drift_acknowledged"


class AuditEvent(StrictModel):
    """One immutable audit record."""

    event_type: EventType
    entity_type: str | None = None
    entity_id: str | None = None
    actor: NonEmptyStr = "system"
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: UtcDatetime = Field(default_factory=utc_now)


class Journal(Protocol):
    """Anything that can append audit events."""

    def append(self, event: AuditEvent) -> None:
        """Store one event. Implementations must never update or delete."""


class InMemoryJournal:
    """Bounded appender used by tests and dry runs."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    def append(self, event: AuditEvent) -> None:
        self._events.append(event)

    def event_types(self) -> tuple[EventType, ...]:
        return tuple(event.event_type for event in self._events)
