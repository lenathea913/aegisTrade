"""Alerting stub: value objects and a log-only transport."""

from __future__ import annotations

from aegis_alerting.alerting import (
    Alert,
    Alerter,
    AlertLevel,
    LoggingAlerter,
    NullAlerter,
    build_alerter,
)

__all__ = [
    "Alert",
    "AlertLevel",
    "Alerter",
    "LoggingAlerter",
    "NullAlerter",
    "build_alerter",
]
