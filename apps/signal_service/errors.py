"""Signal-service errors, mapped to HTTP status codes by the API layer."""

from __future__ import annotations

__all__ = [
    "SignalError",
    "SignalNotFoundError",
    "SignalPositionOpenError",
    "SignalRiskRejectedError",
    "SignalStateError",
]


class SignalError(Exception):
    """Base class for signal problems the caller should see."""


class SignalNotFoundError(SignalError):
    """No signal with that id (404)."""


class SignalStateError(SignalError):
    """The signal cannot change the way the caller asked (409/400)."""


class SignalPositionOpenError(SignalError):
    """A position is already open in this direction, so converting would pyramid (409).

    The runner enforces the same rule before it trades; this is the API/UI half, so an
    operator clicking "convert" cannot do what the daemon refuses to do.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__("Cannot convert: position already open in this direction.")


class SignalRiskRejectedError(SignalError):
    """The risk engine refused the order this signal would become (400)."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__("risk engine rejected the signal: " + ", ".join(reasons))
