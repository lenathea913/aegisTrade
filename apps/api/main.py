"""ASGI entry point: ``uvicorn api.main:app`` (or ``main:app`` with ``--app-dir apps/api``)."""

from __future__ import annotations

from aegis_config import get_settings
from api.app import create_app
from api.instruments import prime

app = create_app()
# Precise prices need the venue's tick sizes. This is the real server entry point;
# tests build the app directly so they never touch the network.
prime(get_settings())
