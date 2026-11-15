"""AegisTrade configuration: typed settings, YAML overlays, env overrides."""

from __future__ import annotations

from aegis_config.loader import (
    CONFIG_DIR,
    REPO_ROOT,
    clear_settings_cache,
    deep_merge,
    get_settings,
    load_settings,
)
from aegis_config.settings import (
    AlertingSettings,
    DatabaseSettings,
    Environment,
    ExchangeSettings,
    ExecutionMode,
    ExecutionSettings,
    MarketDataSettings,
    ObservabilitySettings,
    RedisSettings,
    RiskSettings,
    Settings,
)

__all__ = [
    "CONFIG_DIR",
    "REPO_ROOT",
    "AlertingSettings",
    "DatabaseSettings",
    "Environment",
    "ExchangeSettings",
    "ExecutionMode",
    "ExecutionSettings",
    "MarketDataSettings",
    "ObservabilitySettings",
    "RedisSettings",
    "RiskSettings",
    "Settings",
    "clear_settings_cache",
    "deep_merge",
    "get_settings",
    "load_settings",
]
