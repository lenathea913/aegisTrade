"""Alerting stub.

The MVP ships a *stub*, not a notifier: when alerting is disabled (the default)
``NullAlerter`` swallows alerts, and when it is enabled ``LoggingAlerter`` writes
them to the structured log. No network call, no bot token, no webhook is ever made
here — a real Telegram/Slack transport is deliberately out of scope until the
operator wiring (FASE 9/10) exists.

Alerts are plain value objects so a future transport can be added without touching
the callers.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import Field

from aegis_config import AlertingSettings
from aegis_core import StrictModel, utc_now
from aegis_core.types import NonEmptyStr, UtcDatetime
from aegis_observability import get_logger

__all__ = ["Alert", "AlertLevel", "Alerter", "LoggingAlerter", "NullAlerter", "build_alerter"]


class AlertLevel(StrEnum):
    """How urgent an alert is."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Alert(StrictModel):
    """One operational message."""

    level: AlertLevel = AlertLevel.WARNING
    title: NonEmptyStr
    message: NonEmptyStr
    source: NonEmptyStr = "aegis"
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: UtcDatetime = Field(default_factory=utc_now)


class Alerter(Protocol):
    """Anything that can deliver an alert."""

    def send(self, alert: Alert) -> bool:
        """Deliver ``alert``; return whether it was actually delivered."""


class NullAlerter:
    """Alerting disabled: nothing is sent and nothing is logged."""

    def send(self, alert: Alert) -> bool:
        return False


class LoggingAlerter:
    """Delivers alerts to the structured log (the MVP's only transport)."""

    def __init__(self) -> None:
        self._logger = get_logger("aegis.alerting")

    def send(self, alert: Alert) -> bool:
        log = {
            AlertLevel.CRITICAL: self._logger.critical,
            AlertLevel.WARNING: self._logger.warning,
            AlertLevel.INFO: self._logger.info,
        }[alert.level]
        log(
            "alert",
            alert_level=alert.level.value,
            title=alert.title,
            message=alert.message,
            source=alert.source,
            context=alert.context,
            created_at=alert.created_at.isoformat(),
        )
        return True


def build_alerter(settings: AlertingSettings) -> Alerter:
    """Enabled -> log-only stub; disabled -> silent no-op."""
    return LoggingAlerter() if settings.enabled else NullAlerter()
