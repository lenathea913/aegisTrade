"""Execution errors."""

from __future__ import annotations

from aegis_core import AegisError

__all__ = [
    "ExchangeAuthError",
    "ExchangeError",
    "ExchangeUnavailableError",
    "ExecutionError",
    "LiveTradingDisabledError",
    "NonTestnetEndpointError",
]


class ExecutionError(AegisError):
    """An order could not be turned into a broker instruction."""


class LiveTradingDisabledError(ExecutionError):
    """Live routing was attempted while it is switched off (the MVP default)."""


class ExchangeError(ExecutionError):
    """The exchange refused, timed out or answered with something unusable."""


class ExchangeAuthError(ExchangeError):
    """Credentials were rejected (HTTP 401/403 or a signature error)."""


class ExchangeUnavailableError(ExchangeError):
    """The exchange could not be reached (timeout, DNS, connection reset)."""


class NonTestnetEndpointError(ExecutionError):
    """A non-testnet base URL was configured without the explicit live confirmation."""
