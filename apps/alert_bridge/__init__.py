"""Prometheus alert forwarding for AegisTrade."""

from __future__ import annotations

from alert_bridge.bridge import AlertBridge, alert_from_firing

__all__ = ["AlertBridge", "alert_from_firing"]
