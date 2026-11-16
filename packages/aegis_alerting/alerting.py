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

__all__ = [
    "Alert",
    "AlertLevel",
    "Alerter",
    "LoggingAlerter",
    "NullAlerter",
    "alert_for_rejection",
    "build_alerter",
]


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


def alert_for_rejection(
    *, symbol: str, side: str, reasons: tuple[str, ...], alerter: Alerter | None
) -> None:
    """Notify a risk rejection, promoting kill-switch and daily-loss cases.

    This is the one place that maps a ``RiskDecision`` to an alert, so every caller
    (gate, pipeline, runner) says the same thing about the same rejection.
    """
    if alerter is None:
        return
    joined = ",".join(reasons)
    if any(reason.startswith("kill_switch_active") for reason in reasons):
        alert = Alert(
            level=AlertLevel.CRITICAL,
            title="Kill switch active",
            message="Trading is halted: the kill switch rejected this order.",
            source="risk",
            context={
                "symbol": symbol,
                "side": side,
                "status": "REJECTED",
                "reason": "kill_switch_active",
            },
        )
    elif any(reason.startswith("daily_loss_limit_reached") for reason in reasons):
        alert = Alert(
            level=AlertLevel.CRITICAL,
            title="Daily loss limit reached",
            message="The daily loss limit was touched; new risk is blocked for the day.",
            source="risk",
            context={"symbol": symbol, "side": side, "status": "REJECTED", "reason": joined},
        )
    else:
        alert = Alert(
            level=AlertLevel.WARNING,
            title="Order rejected by risk engine",
            message="The risk engine refused this intent.",
            source="risk",
            context={"symbol": symbol, "side": side, "status": "REJECTED", "reason": joined},
        )
    alerter.send(alert)


def build_alerter(settings: AlertingSettings) -> Alerter:
    """Telegram when configured, the log when only logging is available, silence if off."""
    if not settings.enabled:
        return NullAlerter()
    if "telegram" in settings.channels:
        from aegis_alerting.telegram import TelegramAlerter

        assert settings.telegram_bot_token is not None  # validator guarantees this
        assert settings.telegram_chat_id is not None
        return TelegramAlerter(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            rate_limit_seconds=settings.rate_limit_seconds,
            dedupe_seconds=settings.dedupe_seconds,
        )
    return LoggingAlerter()
