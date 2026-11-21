"""The bridge that gives the Prometheus rules a delivery path."""

from __future__ import annotations

from typing import Any, cast

import httpx

from aegis_alerting import Alert, AlertLevel
from alert_bridge import AlertBridge


class RecordingAlerter:
    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads
        self.calls = 0

    def get(self, url: str) -> _Response:
        self.calls += 1
        index = min(self.calls - 1, len(self._payloads) - 1)
        return _Response(self._payloads[index])


def payload(*, state: str = "firing", name: str = "CollectorLag") -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "alerts": [
                {
                    "labels": {
                        "alertname": name,
                        "severity": "critical",
                        "service": "api",
                    },
                    "annotations": {
                        "summary": "Market data is more than 5 minutes old",
                    },
                    "state": state,
                }
            ]
        },
    }


def bridge_for(client: FakeClient, alerter: RecordingAlerter) -> AlertBridge:
    return AlertBridge(
        prometheus_url="http://prometheus:9090",
        alerter=alerter,
        client=cast(httpx.Client, client),
    )


def test_a_new_firing_alert_is_forwarded_once() -> None:
    alerter = RecordingAlerter()
    client = FakeClient([payload(), payload()])
    bridge = bridge_for(client, alerter)

    first = bridge.poll()
    second = bridge.poll()

    assert len(first) == 1
    assert second == ()
    assert [alert.title for alert in alerter.sent] == ["CollectorLag"]
    assert alerter.sent[0].level is AlertLevel.CRITICAL
    assert alerter.sent[0].source == "prometheus"
    assert alerter.sent[0].context["service"] == "api"


def test_an_alert_that_resolves_and_fires_again_is_forwarded_again() -> None:
    alerter = RecordingAlerter()
    client = FakeClient([payload(), payload(state="inactive"), payload()])
    bridge = bridge_for(client, alerter)

    assert len(bridge.poll()) == 1
    assert bridge.poll() == ()
    assert len(bridge.poll()) == 1
    assert len(alerter.sent) == 2


def test_prometheus_being_down_never_raises() -> None:
    class BrokenClient:
        def get(self, url: str) -> _Response:
            raise httpx.ConnectError("refused")

    alerter = RecordingAlerter()
    bridge = AlertBridge(
        prometheus_url="http://prometheus:9090",
        alerter=alerter,
        client=cast(httpx.Client, BrokenClient()),
    )

    assert bridge.poll() == ()
    assert alerter.sent == []


def test_severity_maps_to_the_alert_level() -> None:
    from alert_bridge import alert_from_firing

    item = payload()["data"]["alerts"][0]
    item["labels"]["severity"] = "warning"
    assert alert_from_firing(item).level is AlertLevel.WARNING

    item["labels"]["severity"] = "critical"
    assert alert_from_firing(item).level is AlertLevel.CRITICAL

    item["labels"]["severity"] = "something-else"
    assert alert_from_firing(item).level is AlertLevel.WARNING
