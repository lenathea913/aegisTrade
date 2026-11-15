"""AegisTrade REST API (read-only in this phase)."""

from __future__ import annotations

from api.app import create_app
from api.service import ApiService

__all__ = ["ApiService", "create_app"]
