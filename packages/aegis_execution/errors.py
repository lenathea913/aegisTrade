"""Execution errors."""

from __future__ import annotations

from aegis_core import AegisError

__all__ = ["ExecutionError", "LiveTradingDisabledError"]


class ExecutionError(AegisError):
    """An order could not be turned into a broker instruction."""


class LiveTradingDisabledError(ExecutionError):
    """Live routing was attempted while it is switched off (the MVP default)."""
