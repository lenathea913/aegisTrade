"""Typed configuration for every AegisTrade service.

Precedence (highest first):

1. environment variables (``AEGIS_*``, nested with ``__``)
2. ``.env`` file
3. YAML overlay: ``configs/base.yaml`` then ``configs/<environment>.yaml``
4. code defaults

Environment variables win over YAML on purpose: a container or operator can always
override a checked-in config, and secrets never need to live in YAML.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from aegis_core import RiskLimit
from aegis_core.enums import Timeframe
from aegis_core.types import SymbolStr

__all__ = [
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
]


class Environment(StrEnum):
    """Deployment environment, selecting the YAML overlay."""

    DEV = "dev"
    PAPER = "paper"
    LIVE = "live"


class ExecutionMode(StrEnum):
    """How orders leave the platform. ``live`` stays inert in the MVP."""

    PAPER = "paper"
    LIVE = "live"


class _Section(BaseModel):
    """Base for config sections: closed, immutable, defaults validated."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class DatabaseSettings(_Section):
    """PostgreSQL connection. TimescaleDB works through the same DSN."""

    url: str
    connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    pool_size: int = Field(default=5, ge=1, le=50)
    max_overflow: int = Field(default=5, ge=0, le=50)
    pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    echo: bool = False

    @field_validator("url")
    @classmethod
    def _require_postgres(cls, value: str) -> str:
        if not value.startswith(("postgresql+psycopg://", "postgresql://")):
            raise ValueError(
                "database.url must be a PostgreSQL DSN (postgresql+psycopg://...); "
                "all timestamps are stored as timestamptz"
            )
        return value


class RedisSettings(_Section):
    """Redis is queue/cache only — never a source of truth for money."""

    url: str
    socket_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    @field_validator("url")
    @classmethod
    def _require_redis(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError("redis.url must start with redis:// or rediss://")
        return value


class ExecutionSettings(_Section):
    """Broker selection.

    ``allow_live_orders`` is a second, independent switch: even with
    ``mode: live``, the execution gateway refuses to send real orders while it is
    false. Live mode also requires exchange credentials (see ``Settings``).
    """

    mode: ExecutionMode = ExecutionMode.PAPER
    broker: str = "paper"
    allow_live_orders: bool = False
    fee_bps: Decimal = Field(default=Decimal("5"), ge=0, le=1000)
    slippage_bps: Decimal = Field(default=Decimal("5"), ge=0, le=1000)

    @model_validator(mode="after")
    def _broker_matches_mode(self) -> Self:
        expected = "live" if self.mode is ExecutionMode.LIVE else "paper"
        if self.broker != expected:
            raise ValueError(
                f"execution.broker {self.broker!r} does not match execution.mode "
                f"{self.mode.value!r}; expected {expected!r}"
            )
        return self


class MarketDataSettings(_Section):
    """What the collector gathers and how stale a candle may be for trading."""

    exchange: str = Field(default="binance", min_length=1, max_length=32)
    source: Literal["csv", "exchange"] = "csv"
    csv_dir: str = "data/seeds"
    symbols: tuple[SymbolStr, ...] = ("BTC/USDT",)
    timeframes: tuple[Timeframe, ...] = (Timeframe.H1,)
    stale_after_seconds: int = Field(default=60, ge=1, le=3600)
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    max_retries: int = Field(default=5, ge=0, le=10)

    @field_validator("symbols")
    @classmethod
    def _at_least_one_symbol(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("market_data.symbols must list at least one symbol")
        return value

    @field_validator("timeframes")
    @classmethod
    def _at_least_one_timeframe(cls, value: tuple[Timeframe, ...]) -> tuple[Timeframe, ...]:
        if not value:
            raise ValueError("market_data.timeframes must list at least one timeframe")
        return value


class RiskSettings(_Section):
    """Risk limits, the global kill switch and the ledger's starting equity.

    The limits reuse the domain ``RiskLimit`` so configuration and engine cannot
    drift apart. ``initial_equity`` is what a freshly started ledger opens with;
    once positions and realized PnL are loaded, it is the base they are added to.
    """

    kill_switch: bool = False
    initial_equity: Decimal = Field(default=Decimal("10000"), gt=0)
    limits: RiskLimit = RiskLimit()


class ExchangeSettings(_Section):
    """Exchange credentials — read from the environment only, never logged.

    Empty strings are treated as "not configured" so a placeholder ``.env`` does
    not look like a real key.
    """

    id: str = Field(default="binance", min_length=1, max_length=32)
    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None
    testnet: bool = True
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def configured(self) -> bool:
        return self.api_key is not None and self.api_secret is not None


class AlertingSettings(_Section):
    """Optional notifications. Disabled by default; no network calls unless enabled."""

    enabled: bool = False
    channels: tuple[Literal["telegram", "slack"], ...] = ()
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    slack_webhook_url: SecretStr | None = None

    @field_validator("telegram_bot_token", "telegram_chat_id", "slack_webhook_url", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _enabled_channels_are_complete(self) -> Self:
        if not self.enabled:
            return self
        if not self.channels:
            raise ValueError("alerting.enabled requires at least one entry in alerting.channels")
        if "telegram" in self.channels and not (
            self.telegram_bot_token is not None and self.telegram_chat_id is not None
        ):
            raise ValueError("telegram channel requires telegram_bot_token and telegram_chat_id")
        if "slack" in self.channels and self.slack_webhook_url is None:
            raise ValueError("slack channel requires slack_webhook_url")
        return self


class ObservabilitySettings(_Section):
    """Structured logging and Prometheus metrics."""

    service_name: str = Field(default="aegis-trade", min_length=1, max_length=64)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_logs: bool = True
    metrics_enabled: bool = True
    metrics_port: int = Field(default=9101, ge=1024, le=65535)


class Settings(BaseSettings):
    """Root configuration object assembled from YAML + environment."""

    model_config = SettingsConfigDict(
        env_prefix="AEGIS_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
        validate_default=True,
        case_sensitive=False,
    )

    environment: Environment = Environment.DEV
    database: DatabaseSettings
    redis: RedisSettings
    execution: ExecutionSettings = ExecutionSettings()
    market_data: MarketDataSettings = MarketDataSettings()
    risk: RiskSettings = RiskSettings()
    exchange: ExchangeSettings = ExchangeSettings()
    alerting: AlertingSettings = AlertingSettings()
    observability: ObservabilitySettings = ObservabilitySettings()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Environment must beat the YAML values passed in as init kwargs.
        return (env_settings, dotenv_settings, init_settings, file_secret_settings)

    @model_validator(mode="after")
    def _live_requires_credentials(self) -> Self:
        if self.execution.mode is ExecutionMode.LIVE and not self.exchange.configured:
            raise ValueError(
                "live execution mode requires exchange credentials "
                "(AEGIS_EXCHANGE__API_KEY and AEGIS_EXCHANGE__API_SECRET)"
            )
        return self

    def redacted_dump(self) -> dict[str, Any]:
        """Config as JSON-safe data with secrets masked — safe for logs/API."""
        return self.model_dump(mode="json")
