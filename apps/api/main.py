"""ASGI entry point: ``uvicorn api.main:app`` (or ``main:app`` with ``--app-dir apps/api``)."""

from __future__ import annotations

from api.app import create_app

app = create_app()
