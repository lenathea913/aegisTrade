"""Structured logging with secret redaction.

Every service calls ``configure_logging(settings.observability)`` once at startup.
Logs are JSON by default (one object per line, easy for Loki/CloudWatch) and the
redaction processor masks anything whose *key* looks like a credential, plus every
``SecretStr`` value it can see. Redaction happens before rendering, so a secret
cannot leak through a nested dict or a keyword argument.
"""

from __future__ import annotations

import logging
import re
from collections.abc import MutableMapping
from typing import Any, cast

import structlog
from pydantic import SecretStr

from aegis_config import ObservabilitySettings

__all__ = ["configure_logging", "get_logger", "redact", "redact_processor"]

_SENSITIVE_KEY = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|authorization|private[_-]?key)",
    re.IGNORECASE,
)
_MASK = "***"


def redact(value: Any) -> Any:
    """Recursively mask sensitive keys and ``SecretStr`` values."""
    if isinstance(value, SecretStr):
        return _MASK
    if isinstance(value, dict):
        return {
            key: _MASK if _SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


def redact_processor(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Structlog processor: redact the whole event dict before rendering."""
    return cast(MutableMapping[str, Any], redact(dict(event_dict)))


def configure_logging(settings: ObservabilitySettings) -> None:
    """Configure structlog for the process (idempotent, safe in tests)."""
    level = getattr(logging, settings.log_level)
    logging.basicConfig(format="%(message)s", level=level, force=True)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.json_logs
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    """Bound logger for ``name``; typed loosely so callers can log freely."""
    return structlog.get_logger(name)
