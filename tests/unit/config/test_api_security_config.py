"""Configuration for API auth, the runner cadence and the monitor's price source."""

from __future__ import annotations

import json

import pytest

from aegis_config import load_settings


def test_api_token_comes_from_the_environment_and_stays_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_API_TOKEN", "s3cret-token-value")

    settings = load_settings("paper")

    assert settings.api_token_required is True
    assert settings.api_token is not None
    assert settings.api_token.get_secret_value() == "s3cret-token-value"
    assert "s3cret-token-value" not in json.dumps(settings.redacted_dump())


def test_a_blank_api_token_means_development_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_API_TOKEN", "   ")

    settings = load_settings("paper")

    assert settings.api_token is None
    assert settings.api_token_required is False


def test_runner_and_monitor_defaults() -> None:
    settings = load_settings("paper")

    assert settings.runner.signal_interval_seconds == 300
    assert settings.runner.monitor_interval_seconds == 60
    assert settings.runner.monitor_enabled is True
    # On by default: the testnet 15m feed lags, and a closed-candle-only monitor is
    # blind to a stop crossed inside the bar.
    assert settings.position_monitor.use_live_price is True


def test_runner_cadence_and_live_price_are_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_RUNNER__SIGNAL_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("AEGIS_POSITION_MONITOR__USE_LIVE_PRICE", "true")

    settings = load_settings("paper")

    assert settings.runner.signal_interval_seconds == 120
    assert settings.position_monitor.use_live_price is True
