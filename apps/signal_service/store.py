"""Where signals live.

A signal is ephemeral in intent — it expires, and a converted one is a draft, not a
trade — but it must survive a restart. Replaying the same closed bar after a restart
used to produce a second, identical idea; with ``trading.signals`` the bar is part of
the key, and the insert is an upsert that does nothing when the row already exists.

The database is the deployment store; ``InMemorySignalStore`` remains for tests and any
caller that has no session factory.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, Table, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from aegis_core import Signal, SignalDirection, SignalStatus, Timeframe
from aegis_db import SignalRecord, session_scope

__all__ = [
    "DEFAULT_SIGNAL_LIST_LIMIT",
    "InMemorySignalStore",
    "SignalStore",
    "SqlSignalStore",
    "signal_from_row",
    "signal_row",
]

#: The dashboard and the expiry sweep only look at recent history.
DEFAULT_SIGNAL_LIST_LIMIT = 200


class SignalStore(Protocol):
    """Minimal signal persistence the service needs."""

    def add(self, signal: Signal) -> None:
        """Store a new signal (idempotent for the same closed bar)."""

    def get(self, signal_id: str) -> Signal | None:
        """Return one signal, or ``None``."""

    def list(self) -> tuple[Signal, ...]:
        """Every known signal, newest first."""

    def update(self, signal: Signal) -> None:
        """Replace a signal with its new status."""


class InMemorySignalStore:
    """Process-local store; newest first, bounded by the service's TTL cleanup."""

    def __init__(self) -> None:
        self._signals: dict[str, Signal] = {}

    def add(self, signal: Signal) -> None:
        self._signals[signal.signal_id] = signal

    def get(self, signal_id: str) -> Signal | None:
        return self._signals.get(signal_id)

    def list(self) -> tuple[Signal, ...]:
        return tuple(sorted(self._signals.values(), key=lambda item: item.created_at, reverse=True))

    def update(self, signal: Signal) -> None:
        if signal.signal_id not in self._signals:
            raise KeyError(signal.signal_id)
        self._signals[signal.signal_id] = signal


def _bar_close_time(signal: Signal) -> datetime:
    """The closed bar the idea came from (``created_at`` if the context lacks it)."""
    raw = signal.market_context.get("quote_as_of")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return signal.created_at


def signal_row(signal: Signal) -> dict[str, Any]:
    """Map a ``Signal`` to ``trading.signals`` column values."""
    return {
        "signal_id": signal.signal_id,
        "symbol": signal.symbol,
        "timeframe": signal.timeframe.value,
        "strategy_id": signal.strategy_id,
        "strategy_version": signal.strategy_version,
        "direction": signal.direction.value,
        "entry_ref": signal.entry_ref,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "risk_reward": signal.risk_reward,
        "bar_close_time": _bar_close_time(signal),
        "reason_codes": list(signal.reason_codes),
        "market_context": dict(signal.market_context),
        "status": signal.status.value,
        "status_reasons": list(signal.status_reasons),
        "created_at": signal.created_at,
        "expires_at": signal.expires_at,
    }


def signal_from_row(row: SignalRecord) -> Signal:
    """Rebuild the domain value from a stored row."""
    context: Mapping[str, Any] = row.market_context or {}
    return Signal(
        signal_id=row.signal_id,
        symbol=row.symbol,
        timeframe=Timeframe(row.timeframe),
        strategy_id=row.strategy_id,
        strategy_version=row.strategy_version,
        direction=SignalDirection(row.direction),
        entry_ref=row.entry_ref,
        stop_loss=row.stop_loss,
        take_profit=row.take_profit,
        risk_reward=row.risk_reward,
        reason_codes=tuple(row.reason_codes or ()),
        market_context=dict(context),
        created_at=row.created_at,
        expires_at=row.expires_at,
        status=SignalStatus(row.status),
        status_reasons=tuple(row.status_reasons or ()),
    )


class SqlSignalStore:
    """``trading.signals`` behind the ``SignalStore`` protocol, one session per call."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        list_limit: int = DEFAULT_SIGNAL_LIST_LIMIT,
    ) -> None:
        self._factory = factory
        self._list_limit = list_limit

    def add(self, signal: Signal) -> None:
        """Insert, or do nothing when this closed bar already produced a signal.

        ``DO NOTHING`` rather than ``DO UPDATE`` on purpose: a signal the operator has
        already converted or ignored must not be reset to ``RISK_OK`` by a restart.
        """
        with session_scope(self._factory) as session:
            session.execute(
                pg_insert(cast(Table, SignalRecord.__table__))
                .values(**signal_row(signal))
                .on_conflict_do_nothing(constraint="uq_signals_idea_bar")
            )

    def get(self, signal_id: str) -> Signal | None:
        with session_scope(self._factory) as session:
            row = session.get(SignalRecord, signal_id)
            return None if row is None else signal_from_row(row)

    def list(self) -> tuple[Signal, ...]:
        with session_scope(self._factory) as session:
            rows = (
                session.execute(
                    select(SignalRecord)
                    .order_by(SignalRecord.created_at.desc(), SignalRecord.bar_close_time.desc())
                    .limit(self._list_limit)
                )
                .scalars()
                .all()
            )
            return tuple(signal_from_row(row) for row in rows)

    def update(self, signal: Signal) -> None:
        with session_scope(self._factory) as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(SignalRecord)
                    .where(SignalRecord.signal_id == signal.signal_id)
                    .values(
                        status=signal.status.value,
                        status_reasons=list(signal.status_reasons),
                    )
                ),
            )
            if result.rowcount == 0:
                raise KeyError(signal.signal_id)
