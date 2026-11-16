"""Pre-flight checklist before aiming AegisTrade at a real exchange.

    uv run python scripts/live_readiness_check.py

Checks, in order: exchange ping, credentials, WebSocket feed, database, Redis, and the
configured execution mode. In ``live`` mode the script refuses to continue until the
operator types the exact confirmation phrase — a checklist that can be skipped is not a
checklist. Exit codes: ``0`` all green, ``1`` a check failed, ``2`` confirmation refused.

Nothing here places an order and nothing prints a key or secret.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from aegis_config import ExecutionMode, Settings, load_settings
from aegis_db import create_engine_from_settings
from aegis_execution import ExchangeError, build_broker
from market_streamer.cli import stream_url
from market_streamer.marks import InMemoryMarkStore, RedisMarkStore
from market_streamer.stream import BinanceMarkStream

__all__ = ["CONFIRMATION", "Check", "main", "run_checks"]

CONFIRMATION = "I UNDERSTAND THE RISKS"


@dataclass(frozen=True)
class Check:
    """One line of the checklist."""

    name: str
    ok: bool
    detail: str


def _close(broker: Any) -> None:
    close = getattr(broker, "close", None)
    if callable(close):
        close()


def _broker(settings: Settings) -> Any:
    return build_broker(
        settings.execution, exchange=settings.exchange, live_confirm=settings.live_confirm
    )


def check_mode(settings: Settings) -> Check:
    mode = settings.execution.mode
    broker = settings.execution.broker
    if mode is ExecutionMode.LIVE:
        return Check("mode", True, f"LIVE via {broker} — real money path")
    return Check("mode", True, f"{mode.value} via {broker}")


def check_exchange(settings: Settings) -> Check:
    try:
        broker = _broker(settings)
    except Exception as exc:
        return Check("exchange ping", False, f"broker not configured: {exc}")
    try:
        ok = bool(broker.ping())
    except Exception as exc:
        return Check("exchange ping", False, f"ping failed: {exc}")
    finally:
        _close(broker)
    return Check("exchange ping", ok, "reachable" if ok else "no answer from the venue")


def check_credentials(settings: Settings) -> Check:
    try:
        broker = _broker(settings)
    except Exception as exc:
        return Check("credentials", False, f"broker not configured: {exc}")
    try:
        verify = getattr(broker, "verify_credentials", None)
        if verify is None:
            return Check("credentials", False, "this broker exposes no signed check")
        verify()
        return Check("credentials", True, "signed account request accepted")
    except ExchangeError as exc:
        # The adapter already redacts keys from its messages.
        return Check("credentials", False, str(exc))
    finally:
        _close(broker)


def check_websocket(settings: Settings) -> Check:
    stream = BinanceMarkStream(
        url=stream_url(settings),
        symbols=settings.market_data.symbols,
        store=InMemoryMarkStore(),
    )
    try:
        from websockets.sync.client import connect

        with connect(stream.stream_url, open_timeout=10) as connection:
            raw = connection.recv(timeout=15)
    except Exception as exc:
        return Check("websocket", False, f"feed unreachable: {exc}")
    mark = stream.handle(raw)
    if mark is None:
        return Check("websocket", False, "connected but the first message was not a mark")
    return Check("websocket", True, f"{mark.symbol} @ {mark.price}")


def check_database(settings: Settings) -> Check:
    engine = create_engine_from_settings(settings.database)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return Check("database", True, "SELECT 1 ok")
    except Exception as exc:
        return Check("database", False, f"unavailable: {exc}")
    finally:
        engine.dispose()


def check_redis(settings: Settings) -> Check:
    try:
        store = RedisMarkStore.from_url(
            settings.redis.url, ttl_seconds=settings.streaming.mark_ttl_seconds
        )
        ok = store.ping()
    except Exception as exc:
        return Check("redis", False, f"unavailable: {exc}")
    return Check("redis", ok, "PING ok" if ok else "PING failed")


def run_checks(settings: Settings) -> tuple[Check, ...]:
    """Run every check; each one reports itself instead of raising."""
    checks: tuple[Callable[[Settings], Check], ...] = (
        check_mode,
        check_exchange,
        check_credentials,
        check_websocket,
        check_database,
        check_redis,
    )
    return tuple(check(settings) for check in checks)


def _confirm_live(settings: Settings, ask: Callable[[str], str]) -> bool:
    if settings.execution.mode is not ExecutionMode.LIVE:
        return True
    print(
        "\n*** execution.mode is LIVE: orders would use REAL money. ***\n"
        f"Type exactly: {CONFIRMATION}\n"
    )
    return ask("> ").strip() == CONFIRMATION


def main(argv: Sequence[str] | None = None, *, ask: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(description="AegisTrade live readiness checklist")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="deprecated no-op: the live confirmation always has to be typed",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.yes:
        print("--yes is ignored: live mode always requires the typed confirmation.")

    if not _confirm_live(settings, ask):
        print("Confirmation not given; stopping before any check.")
        return 2

    checks = run_checks(settings)
    width = max(len(check.name) for check in checks)
    for check in checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"[{status}] {check.name.ljust(width)}  {check.detail}")

    if settings.streaming.enabled is False:
        print(
            "[WARN] streaming.enabled is false: live-price exits fall back to closed "
            "candles, and the risk gate will reject stale quotes."
        )
    if not settings.alerting.enabled:
        print("[WARN] alerting is disabled: filled/rejected orders will not notify anyone.")

    failed = [check for check in checks if not check.ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed.")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
