"""Alerting stub: value objects and a log-only transport."""

from __future__ import annotations

from aegis_alerting.alerting import (
    Alert,
    Alerter,
    AlertLevel,
    LoggingAlerter,
    NullAlerter,
    alert_for_rejection,
    build_alerter,
)
from aegis_alerting.telegram import TelegramAlerter, format_alert

__all__ = [
    "Alert",
    "AlertLevel",
    "Alerter",
    "LoggingAlerter",
    "NullAlerter",
    "TelegramAlerter",
    "alert_for_rejection",
    "build_alerter",
    "format_alert",
]
