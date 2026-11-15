"""YAML overlay loading.

``configs/base.yaml`` provides the defaults for every environment;
``configs/<environment>.yaml`` is deep-merged on top. Environment variables are
applied last by pydantic-settings (see ``Settings.settings_customise_sources``).

A config loader that silently ignores a typo is worse than no config at all, so
the merged document is validated strictly (``extra=forbid`` everywhere).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from aegis_config.settings import Environment, Settings

__all__ = [
    "CONFIG_DIR",
    "REPO_ROOT",
    "clear_settings_cache",
    "deep_merge",
    "get_settings",
    "load_settings",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"

_ENV_VAR = "AEGIS_ENVIRONMENT"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise TypeError(f"config file {path} must contain a YAML mapping at the top level")
    return data


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base`` without mutating either."""
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def load_settings(
    environment: str | Environment | None = None,
    *,
    config_dir: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Load ``base.yaml`` + ``<environment>.yaml`` and validate the result.

    Args:
        environment: environment name; defaults to ``AEGIS_ENVIRONMENT`` or ``dev``.
        config_dir: directory holding the YAML files (defaults to ``<repo>/configs``).
        overrides: extra mapping merged last, before pydantic validation (tests/tools).

    Raises:
        FileNotFoundError: when ``base.yaml`` or the overlay is missing.
        pydantic.ValidationError: when the merged document is invalid.
    """
    resolved_env = Environment(
        environment if environment is not None else os.environ.get(_ENV_VAR, "dev")
    )
    directory = config_dir if config_dir is not None else CONFIG_DIR

    merged = deep_merge(
        _read_yaml(directory / "base.yaml"), _read_yaml(directory / f"{resolved_env}.yaml")
    )
    merged["environment"] = resolved_env.value
    if overrides is not None:
        merged = deep_merge(merged, overrides)

    return Settings(**merged)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (see ``clear_settings_cache`` in tests)."""
    return load_settings()


def clear_settings_cache() -> None:
    """Drop the cached settings so the next ``get_settings()`` re-reads config."""
    get_settings.cache_clear()
