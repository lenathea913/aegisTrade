"""Signal domain service: generate ideas, preview risk, convert to a draft intent.

Nothing in this package can place an order: conversion returns an ``OrderIntent`` and
the risk engine runs again on the way, so a signal can never skip ADR 0003.
"""

from __future__ import annotations

from signal_service.errors import (
    SignalError,
    SignalNotFoundError,
    SignalPositionOpenError,
    SignalRiskRejectedError,
    SignalStateError,
)
from signal_service.repository import CandleRepository, SqlCandleRepository
from signal_service.service import (
    DEFAULT_HISTORY_BARS,
    DEFAULT_SIGNAL_TTL_SECONDS,
    ConversionResult,
    SignalService,
)
from signal_service.store import InMemorySignalStore, SignalStore, SqlSignalStore

__all__ = [
    "DEFAULT_HISTORY_BARS",
    "DEFAULT_SIGNAL_TTL_SECONDS",
    "CandleRepository",
    "ConversionResult",
    "InMemorySignalStore",
    "SignalError",
    "SignalNotFoundError",
    "SignalPositionOpenError",
    "SignalRiskRejectedError",
    "SignalService",
    "SignalStateError",
    "SignalStore",
    "SqlCandleRepository",
    "SqlSignalStore",
]
