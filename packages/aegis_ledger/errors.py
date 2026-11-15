"""Ledger errors."""

from __future__ import annotations

from aegis_core import AegisError

__all__ = ["LedgerError"]


class LedgerError(AegisError):
    """The ledger was asked to do something it cannot reconcile."""
