"""Where signals live.

Signals are ephemeral by design: they expire, and a converted one is a draft, not a
trade. The MVP therefore keeps them in process memory behind a protocol, so a future
``signal`` table can replace the implementation without touching the service.
"""

from __future__ import annotations

from typing import Protocol

from aegis_core import Signal

__all__ = ["InMemorySignalStore", "SignalStore"]


class SignalStore(Protocol):
    """Minimal signal persistence the service needs."""

    def add(self, signal: Signal) -> None:
        """Store a new signal."""

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
