"""Telegram delivery for alerts.

The transport is deliberately small and defensive:

* **The token never leaves the URL path.** It is read from a ``SecretStr`` only when
  building the request, and no exception message, log record or alert body ever
  contains it. Context keys that look like credentials are dropped before formatting.
* **One message at a time.** ``rate_limit_seconds`` is a minimum gap between sends and
  ``dedupe_seconds`` collapses an identical alert that repeats — a dead exchange that
  fails every five seconds must not become a five-second stream of messages.
* **Delivery failures never raise.** An alerting outage is not a trading incident; the
  caller gets ``False`` and the log gets one warning.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import SecretStr

from aegis_alerting.alerting import Alert
from aegis_observability import get_logger

__all__ = ["TelegramAlerter", "format_alert"]

#: Context keys that must never reach a chat. The adapter drops them defensively even
#: though callers are expected not to pass them in the first place.
_SECRET_HINTS = ("secret", "token", "api_key", "apikey", "password")
_ESCAPE = str.maketrans({"_": r"\_", "*": r"\*", "[": r"\[", "]": r"\]", "`": r"\`"})
#: Field order for the message body: the five things an operator reads first.
_FIELD_ORDER = ("symbol", "side", "status", "price", "reason")


def _escape(value: Any) -> str:
    return str(value).translate(_ESCAPE)


def format_alert(alert: Alert) -> str:
    """Render one alert as Telegram Markdown (symbol, side, status, price, reason)."""
    lines = [f"*{_escape(alert.title)}*"]
    context = {
        key: value
        for key, value in alert.context.items()
        if not any(hint in key.lower() for hint in _SECRET_HINTS)
    }
    for key in _FIELD_ORDER:
        if key in context:
            # Inside a code span nothing needs escaping, and escaping would show up
            # literally in the chat (``simple\_ema\_cross``).
            lines.append(f"{key.capitalize()}: `{context.pop(key)}`")
    lines.append("")
    lines.append(_escape(alert.message))
    for key, value in context.items():
        lines.append(f"{_escape(key.capitalize())}: {_escape(value)}")
    return "\n".join(lines)


class TelegramAlerter:
    """Sends alerts through the Telegram Bot API."""

    name = "telegram"

    def __init__(
        self,
        *,
        bot_token: SecretStr | str,
        chat_id: str,
        client: httpx.Client | None = None,
        api_base: str = "https://api.telegram.org",
        rate_limit_seconds: float = 2.0,
        dedupe_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        token = bot_token.get_secret_value() if isinstance(bot_token, SecretStr) else bot_token
        if not token or not chat_id:
            raise ValueError("telegram alerting needs a bot token and a chat id")
        self._token = token
        self._chat_id = chat_id
        self._api_base = api_base.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=5.0)
        self._rate_limit = max(rate_limit_seconds, 0.0)
        self._dedupe_seconds = max(dedupe_seconds, 0.0)
        self._clock = clock
        self._last_sent: float | None = None
        self._recent: dict[str, float] = {}
        self._logger = get_logger("aegis.alerting")

    def close(self) -> None:
        """Close the HTTP client, but only when this alerter created it."""
        if self._owns_client:
            self._client.close()

    def send(self, alert: Alert) -> bool:
        """Deliver ``alert``; ``False`` when throttled, deduplicated or rejected."""
        now = self._clock()
        key = f"{alert.source}|{alert.title}|{alert.message}"
        last_seen = self._recent.get(key)
        if last_seen is not None and now - last_seen < self._dedupe_seconds:
            self._logger.info("telegram_alert_suppressed", title=alert.title, reason="duplicate")
            return False
        if self._last_sent is not None and now - self._last_sent < self._rate_limit:
            self._logger.info("telegram_alert_suppressed", title=alert.title, reason="rate_limit")
            return False

        # The token lives only in this path; it is never logged, raised or formatted.
        url = f"{self._api_base}/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": format_alert(alert),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            response = self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            self._logger.warning(
                "telegram_delivery_failed", title=alert.title, error=type(exc).__name__
            )
            return False
        if response.status_code >= 400:
            self._logger.warning(
                "telegram_delivery_failed", title=alert.title, status=response.status_code
            )
            return False

        self._last_sent = now
        self._recent = {
            seen: at for seen, at in self._recent.items() if now - at < self._dedupe_seconds
        }
        self._recent[key] = now
        return True
