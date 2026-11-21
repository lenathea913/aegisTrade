"""Forward Prometheus firing alerts to the platform's alerter.

The safety rules live in ``infra/prometheus/rules.yml``. Prometheus evaluates them but
cannot deliver anything itself, and this project has no Alertmanager; the bridge is the
missing link. It polls the Prometheus alerts API and hands each *newly* firing alert to
``build_alerter`` — Telegram when configured, the structured log otherwise — so the same
``TelegramAlerter`` the risk gate uses is the only transport that ever leaves the host.

Only the transition to firing is sent. Re-sending every poll would turn one stale feed
into a message every 30 seconds; Prometheus still reports the alert as firing for anyone
looking at its UI.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from aegis_alerting import Alert, Alerter, AlertLevel
from aegis_observability import get_logger

__all__ = ["AlertBridge", "alert_from_firing"]

_LEVELS = {
    "critical": AlertLevel.CRITICAL,
    "warning": AlertLevel.WARNING,
    "info": AlertLevel.INFO,
}


def _mapping(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _fingerprint(item: dict[str, Any]) -> str:
    """Identity of an alert across polls: name plus its full label set."""
    labels = _mapping(item, "labels")
    return json.dumps(
        {"alertname": labels.get("alertname"), "labels": labels}, sort_keys=True, default=str
    )


def alert_from_firing(item: dict[str, Any]) -> Alert:
    """Map one entry of ``/api/v1/alerts`` to a domain ``Alert``."""
    labels = _mapping(item, "labels")
    annotations = _mapping(item, "annotations")
    name = _text(labels.get("alertname") or "PrometheusAlert")
    severity = _text(labels.get("severity") or "warning").lower()
    message = _text(
        annotations.get("summary") or annotations.get("description") or "Prometheus rule firing."
    )
    context = {str(key): _text(value) for key, value in labels.items()}
    context["status"] = "FIRING"
    return Alert(
        level=_LEVELS.get(severity, AlertLevel.WARNING),
        title=name,
        message=message,
        source="prometheus",
        context=context,
    )


class AlertBridge:
    """Polls Prometheus and forwards alerts that started firing since the last poll."""

    def __init__(
        self,
        *,
        prometheus_url: str,
        alerter: Alerter,
        client: httpx.Client | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._url = prometheus_url.rstrip("/")
        self._alerter = alerter
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._firing: set[str] = set()
        self._logger = get_logger("aegis.alert_bridge")

    def close(self) -> None:
        """Close the HTTP client, but only when this bridge created it."""
        if self._owns_client:
            self._client.close()

    def poll(self) -> tuple[Alert, ...]:
        """Send every newly firing alert; return what was forwarded."""
        payload = self._fetch()
        if payload is None:
            return ()
        data = payload.get("data")
        raw = data.get("alerts") if isinstance(data, dict) else None
        entries = raw if isinstance(raw, list) else []
        firing = [
            item for item in entries if isinstance(item, dict) and item.get("state") == "firing"
        ]

        current = {_fingerprint(item) for item in firing}
        sent: list[Alert] = []
        for item in firing:
            fingerprint = _fingerprint(item)
            if fingerprint in self._firing:
                continue
            alert = alert_from_firing(item)
            self._alerter.send(alert)
            sent.append(alert)
        # A resolved alert may fire again later; forgetting it here is what makes the
        # second occurrence a new notification instead of silence.
        self._firing = current
        return tuple(sent)

    def _fetch(self) -> dict[str, Any] | None:
        try:
            response = self._client.get(f"{self._url}/api/v1/alerts")
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Prometheus being down is not a trading incident: the rules simply cannot
            # be forwarded this cycle. Never raise into the caller's loop.
            self._logger.warning("alert_bridge_fetch_failed", url=self._url, error=str(exc))
            return None
        return payload if isinstance(payload, dict) else None
