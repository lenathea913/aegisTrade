"""Telegram transport: formatting, rate limiting, dedupe and secret hygiene."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from aegis_alerting import Alert, AlertLevel, TelegramAlerter, format_alert

TOKEN = "123456:bot-token-should-never-leak"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_alerter(
    handler: Callable[[httpx.Request], httpx.Response], clock: Clock | None = None
) -> tuple[TelegramAlerter, list[httpx.Request]]:
    sent: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    alerter = TelegramAlerter(
        bot_token=TOKEN,
        chat_id="42",
        client=httpx.Client(transport=httpx.MockTransport(recording)),
        rate_limit_seconds=2.0,
        dedupe_seconds=60.0,
        clock=clock or Clock(),
    )
    return alerter, sent


def alert() -> Alert:
    return Alert(
        level=AlertLevel.INFO,
        title="Order filled",
        message="BUY 0.1 BTC/USDT at 30000.",
        source="execution",
        context={
            "symbol": "BTC/USDT",
            "side": "BUY",
            "status": "FILLED",
            "price": "30000",
            "reason": "simple_ema_cross",
        },
    )


def test_message_is_markdown_with_the_five_key_fields() -> None:
    text = format_alert(alert())

    assert text.startswith("*Order filled*")
    assert "Symbol: `BTC/USDT`" in text
    assert "Side: `BUY`" in text
    assert "Status: `FILLED`" in text
    assert "Price: `30000`" in text
    assert "Reason: `simple_ema_cross`" in text


def test_send_posts_to_the_bot_endpoint_without_leaking_the_token() -> None:
    alerter, sent = make_alerter(lambda _request: httpx.Response(200, json={"ok": True}))

    assert alerter.send(alert()) is True

    request = sent[0]
    assert request.url.host == "api.telegram.org"
    # The token is part of the path, but never of the body or a heading.
    assert TOKEN in str(request.url)
    body = json.loads(request.content)
    assert body["chat_id"] == "42"
    assert body["parse_mode"] == "Markdown"
    assert TOKEN not in body["text"]
    assert "BTC/USDT" in body["text"]


def test_lookalike_secret_fields_are_dropped_from_the_message() -> None:
    text = format_alert(
        Alert(
            title="config",
            message="check",
            context={"api_secret": "leak-me", "bot_token": "leak-me-too", "symbol": "ETH/USDT"},
        )
    )

    assert "leak-me" not in text
    assert "ETH/USDT" in text


def test_repeated_identical_alerts_are_deduplicated() -> None:
    clock = Clock()
    alerter, sent = make_alerter(lambda _request: httpx.Response(200), clock)

    assert alerter.send(alert()) is True
    clock.advance(5.0)
    assert alerter.send(alert()) is False  # same text inside the dedupe window
    clock.advance(120.0)
    assert alerter.send(alert()) is True

    assert len(sent) == 2


def test_messages_are_rate_limited_even_when_different() -> None:
    clock = Clock()
    alerter, sent = make_alerter(lambda _request: httpx.Response(200), clock)

    first = alert()
    second = Alert(title="Kill switch active", message="halt", source="risk")

    assert alerter.send(first) is True
    clock.advance(0.5)
    assert alerter.send(second) is False  # inside the 2 s minimum gap
    clock.advance(2.0)
    assert alerter.send(second) is True
    assert len(sent) == 2


def test_http_failure_is_reported_not_raised() -> None:
    alerter, _ = make_alerter(lambda _request: httpx.Response(401, json={"ok": False}))

    assert alerter.send(alert()) is False


def test_transport_error_is_reported_not_raised() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    alerter, _ = make_alerter(boom)

    assert alerter.send(alert()) is False


def test_close_leaves_an_injected_client_open() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    alerter = TelegramAlerter(bot_token=TOKEN, chat_id="42", client=client)

    alerter.close()

    assert client.is_closed is False
    client.close()
