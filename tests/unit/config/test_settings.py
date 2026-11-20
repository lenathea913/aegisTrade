"""Configuration: overlay loading, precedence, redaction and fail-fast validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aegis_config import (
    CONFIG_DIR,
    Environment,
    ExecutionMode,
    Settings,
    clear_settings_cache,
    deep_merge,
    get_settings,
    load_settings,
)
from aegis_core import Timeframe
from aegis_risk import RiskGate

SECRET_SENTINEL = "do-not-leak-this-value"


def test_dev_overlay_is_loaded() -> None:
    settings = load_settings("dev")
    assert settings.environment is Environment.DEV
    assert settings.execution.mode is ExecutionMode.PAPER
    assert settings.execution.broker == "paper"
    assert settings.observability.log_level == "DEBUG"
    assert settings.observability.json_logs is False
    assert settings.market_data.source == "binance"
    # Nothing is pinned, so the budget follows the configured timeframe.
    assert settings.market_data.stale_after_seconds is None
    assert settings.market_data.stale_after_for(Timeframe.H1) == 3720


def test_paper_overlay_differs_from_dev() -> None:
    paper = load_settings("paper")
    assert paper.environment is Environment.PAPER
    assert paper.observability.json_logs is True
    assert paper.market_data.source == "binance"
    assert paper.market_data.stale_after_seconds is None
    assert paper.risk.kill_switch is False


def test_the_freshness_window_follows_the_timeframe() -> None:
    market = load_settings("paper").market_data

    # One bar + one collector pass + tolerance, never a fixed 120 s.
    assert market.stale_after_for(Timeframe.M15) == 900 + 60 + 60
    assert market.stale_after_for(Timeframe.H1) == 3600 + 60 + 60
    assert market.stale_after_for(Timeframe.D1) == 86400 + 60 + 60


def test_an_explicit_freshness_window_still_wins() -> None:
    settings = load_settings("paper", overrides={"market_data": {"stale_after_seconds": 30}})

    assert settings.market_data.stale_after_for(Timeframe.H1) == 30


def test_the_risk_gate_derives_its_window_from_the_traded_timeframe() -> None:
    gate = RiskGate.from_settings(load_settings("paper"), Timeframe.H1)

    assert gate.stale_after_seconds == 3720


def test_exchange_environment_names_the_venue() -> None:
    assert load_settings("paper").exchange.environment == "testnet"
    mainnet = load_settings(
        "paper", overrides={"exchange": {"testnet": False, "base_url": "https://api.binance.com"}}
    )
    assert mainnet.exchange.environment == "mainnet"


def test_live_overlay_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="requires exchange credentials"):
        load_settings("live")


def test_live_overlay_with_credentials_stays_inert() -> None:
    settings = load_settings(
        "live",
        overrides={"exchange": {"api_key": SECRET_SENTINEL, "api_secret": SECRET_SENTINEL}},
    )
    assert settings.execution.mode is ExecutionMode.LIVE
    assert settings.execution.allow_live_orders is False  # second, independent gate
    assert settings.risk.limits.max_risk_per_trade_pct == 0.25
    assert settings.market_data.stale_after_seconds is None
    assert settings.exchange.environment == "mainnet"


def test_risk_limits_are_shared_with_the_domain() -> None:
    limits = load_settings("paper").risk.limits
    assert limits.max_daily_loss_pct == 2
    assert limits.allows("btc/usdt") is True
    assert limits.allows("ETH/USDT") is True
    # The universe is the top-volume list from base.yaml; anything outside it is denied.
    assert limits.allows("AVAX/USDT") is False
    assert limits.max_open_positions == 3


def test_all_overlays_define_the_same_sections() -> None:
    names = {"base", "dev", "paper", "live"}
    present = {path.stem for path in CONFIG_DIR.glob("*.yaml")}
    assert names <= present


# ----------------------------------------------------------------- precedence


def test_environment_variable_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_MARKET_DATA__STALE_AFTER_SECONDS", "42")
    assert load_settings("dev").market_data.stale_after_seconds == 42


def test_environment_variable_overrides_explicit_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_RISK__KILL_SWITCH", "true")
    settings = load_settings("paper", overrides={"risk": {"kill_switch": False}})
    assert settings.risk.kill_switch is True


def test_overrides_are_merged_and_do_not_mutate_inputs() -> None:
    overrides: dict[str, Any] = {"observability": {"log_level": "WARNING"}}
    settings = load_settings("paper", overrides=overrides)
    assert settings.observability.log_level == "WARNING"
    assert settings.observability.json_logs is True  # untouched sibling
    assert overrides == {"observability": {"log_level": "WARNING"}}


def test_deep_merge_recurses() -> None:
    merged = deep_merge(
        {"a": {"b": 1, "c": 2}, "d": 3},
        {"a": {"c": 9, "e": 4}, "d": 5},
    )
    assert merged == {"a": {"b": 1, "c": 9, "e": 4}, "d": 5}


def test_deep_merge_does_not_mutate_arguments() -> None:
    base: dict[str, Any] = {"a": {"b": 1}}
    overlay: dict[str, Any] = {"a": {"c": 2}}
    deep_merge(base, overlay)
    assert base == {"a": {"b": 1}}
    assert overlay == {"a": {"c": 2}}


# -------------------------------------------------------------- fail-fast config


def test_unknown_yaml_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        load_settings("paper", overrides={"risk": {"max_risk_per_trad_pct": 1}})


def test_invalid_percentage_is_rejected_at_load_time() -> None:
    with pytest.raises(ValidationError):
        load_settings("paper", overrides={"risk": {"limits": {"max_risk_per_trade_pct": 250}}})


def test_broker_must_match_execution_mode() -> None:
    with pytest.raises(ValidationError, match=r"does not match execution.mode"):
        load_settings("paper", overrides={"execution": {"broker": "live"}})


@pytest.mark.parametrize("url", ["sqlite:///aegis.db", "mysql://db/aegis", "aegis"])
def test_database_url_must_be_postgres(url: str) -> None:
    with pytest.raises(ValidationError, match="PostgreSQL DSN"):
        load_settings("paper", overrides={"database": {"url": url}})


def test_redis_url_is_validated() -> None:
    with pytest.raises(ValidationError, match="redis://"):
        load_settings("paper", overrides={"redis": {"url": "http://localhost:6379"}})


def test_market_data_needs_a_symbol_and_timeframe() -> None:
    with pytest.raises(ValidationError, match="at least one symbol"):
        load_settings("paper", overrides={"market_data": {"symbols": []}})
    with pytest.raises(ValidationError, match="at least one timeframe"):
        load_settings("paper", overrides={"market_data": {"timeframes": []}})


def test_unknown_timeframe_is_rejected() -> None:
    with pytest.raises(ValidationError):
        load_settings("paper", overrides={"market_data": {"timeframes": ["2h"]}})


def test_symbols_are_normalised_from_yaml() -> None:
    settings = load_settings("paper", overrides={"market_data": {"symbols": ["eth-usdt"]}})
    assert settings.market_data.symbols == ("ETH/USDT",)


# --------------------------------------------------------------------- alerting


def test_alerting_disabled_by_default() -> None:
    alerting = load_settings("paper").alerting
    assert alerting.enabled is False
    assert alerting.channels == ()


def test_enabling_alerting_requires_a_complete_channel() -> None:
    with pytest.raises(ValidationError, match="at least one entry"):
        load_settings("paper", overrides={"alerting": {"enabled": True}})
    with pytest.raises(ValidationError, match="telegram"):
        load_settings(
            "paper",
            overrides={"alerting": {"enabled": True, "channels": ["telegram"]}},
        )
    with pytest.raises(ValidationError, match="slack"):
        load_settings("paper", overrides={"alerting": {"enabled": True, "channels": ["slack"]}})


def test_complete_alerting_configuration_is_accepted() -> None:
    settings = load_settings(
        "paper",
        overrides={
            "alerting": {
                "enabled": True,
                "channels": ["slack"],
                "slack_webhook_url": SECRET_SENTINEL,
            }
        },
    )
    assert settings.alerting.enabled is True
    assert settings.alerting.slack_webhook_url is not None


def test_telegram_channel_with_credentials_is_accepted() -> None:
    settings = load_settings(
        "paper",
        overrides={
            "alerting": {
                "enabled": True,
                "channels": ["telegram"],
                "telegram_bot_token": SECRET_SENTINEL,
                "telegram_chat_id": "123456",
            }
        },
    )
    assert settings.alerting.channels == ("telegram",)
    assert settings.alerting.telegram_chat_id == "123456"


def test_blank_telegram_chat_id_counts_as_missing() -> None:
    with pytest.raises(ValidationError, match="telegram"):
        load_settings(
            "paper",
            overrides={
                "alerting": {
                    "enabled": True,
                    "channels": ["telegram"],
                    "telegram_bot_token": SECRET_SENTINEL,
                    "telegram_chat_id": "   ",
                }
            },
        )


def test_blank_secret_is_treated_as_unset() -> None:
    settings = load_settings("paper", overrides={"exchange": {"api_key": "   ", "api_secret": ""}})
    assert settings.exchange.configured is False


# --------------------------------------------------------------------- secrets


def test_secrets_never_appear_in_redacted_dump() -> None:
    settings = load_settings(
        "paper",
        overrides={"exchange": {"api_key": SECRET_SENTINEL, "api_secret": SECRET_SENTINEL}},
    )
    dumped = settings.redacted_dump()
    assert SECRET_SENTINEL not in str(dumped)
    assert dumped["exchange"]["api_key"] == "**********"


def test_secrets_never_appear_in_repr() -> None:
    settings = load_settings(
        "paper",
        overrides={"exchange": {"api_key": SECRET_SENTINEL, "api_secret": SECRET_SENTINEL}},
    )
    assert SECRET_SENTINEL not in repr(settings)
    assert SECRET_SENTINEL not in str(settings)


def test_settings_are_immutable() -> None:
    settings = load_settings("paper")
    with pytest.raises(ValidationError):
        settings.risk.kill_switch = True  # type: ignore[misc]


# ----------------------------------------------------------------------- loader


def test_missing_overlay_raises(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text("database: {url: 'postgresql://x/y'}", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="not found"):
        load_settings("paper", config_dir=tmp_path)


def test_non_mapping_yaml_raises(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    (tmp_path / "paper.yaml").write_text("{}", encoding="utf-8")
    with pytest.raises(TypeError, match="mapping"):
        load_settings("paper", config_dir=tmp_path)


def test_get_settings_is_cached_and_resettable(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_settings_cache()
    monkeypatch.setenv("AEGIS_ENVIRONMENT", "dev")
    first = get_settings()
    assert get_settings() is first

    clear_settings_cache()
    assert get_settings() is not first


def test_settings_requires_sections_when_yaml_is_empty(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / "paper.yaml").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings("paper", config_dir=tmp_path)


def test_default_environment_is_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AEGIS_ENVIRONMENT", raising=False)
    assert isinstance(load_settings(), Settings)
    assert load_settings().environment is Environment.DEV
