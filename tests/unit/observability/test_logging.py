"""Structured logging: JSON records and secret redaction."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from aegis_config import ObservabilitySettings
from aegis_observability import configure_logging, get_logger, redact
from aegis_observability.logging import redact_processor


def test_redact_masks_sensitive_keys_recursively() -> None:
    payload = {
        "event": "order",
        "api_key": "abc",
        "nested": {"telegram_bot_token": "123", "amount": "10"},
        "items": [{"password": "x"}, {"symbol": "BTC/USDT"}],
    }

    masked = redact(payload)

    assert masked["event"] == "order"
    assert masked["api_key"] == "***"
    assert masked["nested"] == {"telegram_bot_token": "***", "amount": "10"}
    assert masked["items"] == [{"password": "***"}, {"symbol": "BTC/USDT"}]


def test_redact_masks_secret_str_values() -> None:
    assert redact({"anything": SecretStr("s3cret")}) == {"anything": "***"}


def test_redact_processor_returns_a_masked_dict() -> None:
    processed = redact_processor(None, "info", {"event": "x", "api_secret": "shh"})

    assert processed == {"event": "x", "api_secret": "***"}


def test_json_logs_hide_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(ObservabilitySettings(log_level="INFO", json_logs=True))
    logger = get_logger("aegis.test")

    logger.info("paper_start", api_key=SecretStr("super-secret"), symbol="BTC/USDT")

    out = capsys.readouterr().out
    record = json.loads(out.strip())
    assert record["event"] == "paper_start"
    assert record["level"] == "info"
    assert record["symbol"] == "BTC/USDT"
    assert record["api_key"] == "***"
    assert "super-secret" not in out
    assert record["timestamp"]


def test_console_logs_are_used_in_dev(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(ObservabilitySettings(log_level="DEBUG", json_logs=False))
    logger = get_logger("aegis.test")

    logger.debug("hello_console", symbol="BTC/USDT")

    out = capsys.readouterr().out
    assert "hello_console" in out
    assert "{" not in out
