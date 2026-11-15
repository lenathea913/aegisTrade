"""Alerting stub: disabled means silent, enabled means log-only."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from aegis_alerting import Alert, AlertLevel, LoggingAlerter, NullAlerter, build_alerter
from aegis_config import AlertingSettings, ObservabilitySettings
from aegis_observability import configure_logging


def test_null_alerter_delivers_nothing() -> None:
    alert = Alert(title="kill switch", message="operator halted trading")

    assert NullAlerter().send(alert) is False


def test_logging_alerter_writes_one_structured_record(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(ObservabilitySettings(log_level="INFO", json_logs=True))
    alert = Alert(
        level=AlertLevel.CRITICAL,
        title="daily loss limit",
        message="daily loss limit reached",
        context={"equity": "9800"},
    )

    assert LoggingAlerter().send(alert) is True

    record = json.loads(capsys.readouterr().out.strip())
    assert record["event"] == "alert"
    assert record["level"] == "critical"
    assert record["alert_level"] == "critical"
    assert record["title"] == "daily loss limit"
    assert record["context"] == {"equity": "9800"}


def test_build_alerter_follows_configuration() -> None:
    disabled = build_alerter(AlertingSettings())
    assert isinstance(disabled, NullAlerter)

    enabled = build_alerter(
        AlertingSettings(
            enabled=True,
            channels=("slack",),
            slack_webhook_url="https://example.invalid/hook",
        )
    )
    assert isinstance(enabled, LoggingAlerter)


def test_alert_requires_content() -> None:
    with pytest.raises(ValidationError):
        Alert(title="   ", message="something happened")

    alert = Alert(title="t", message="m")
    assert alert.level is AlertLevel.WARNING
    assert alert.source == "aegis"
