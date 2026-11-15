"""Prefixed identifiers.

Identifiers are opaque, sortable-enough strings with a human-readable prefix so log
lines and audit rows can be read at a glance (``ord_...``, ``trd_...``).
"""

from __future__ import annotations

import uuid
from typing import Final

_PREFIXES: Final[frozenset[str]] = frozenset(
    {"ord", "trd", "pos", "int", "appr", "risk", "evt", "run", "sig"}
)


def new_id(prefix: str) -> str:
    """Return ``<prefix>_<32 hex chars>``.

    Args:
        prefix: short lowercase tag such as ``"ord"``.

    Raises:
        ValueError: if the prefix is empty, too long, or contains odd characters.
    """
    if prefix not in _PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}; allowed: {sorted(_PREFIXES)}")
    return f"{prefix}_{uuid.uuid4().hex}"
