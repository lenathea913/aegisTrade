"""Shared strict base model for AegisTrade domain objects.

Domain objects are **immutable** (frozen), reject unknown fields, and normalise
whitespace. Immutability matters for auditability: once an ``OrderIntent`` has
been approved, neither the strategy nor the risk engine can quietly mutate it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Frozen, closed pydantic model used by every domain type."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
        validate_assignment=False,
    )
