"""Domain exceptions.

Only what the domain itself needs. Service-level errors (broker, ledger, API) are
defined next to the code that raises them.
"""

from __future__ import annotations


class AegisError(Exception):
    """Base class for every AegisTrade error."""


class DomainError(AegisError):
    """A domain invariant was violated (bad value, impossible state)."""


class InvalidStateTransitionError(DomainError):
    """An object was asked to move to a state that is not reachable from here."""


class UnapprovedOrderError(DomainError):
    """Raised when execution is attempted with anything but an ``ApprovedOrder``."""
