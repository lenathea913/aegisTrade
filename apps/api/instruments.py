"""Venue metadata: instrument precision (tick size / lot step) for the dashboard.

Priming happens once at process start (``apps/api/main.py``) and never blocks the
request path: if the venue is unreachable the registry keeps its fallback, the
dashboard still renders, and the log says why the precision is generic.
"""

from __future__ import annotations

from aegis_config import Settings
from aegis_marketdata import (
    BINANCE_MAINNET_BASE_URL,
    BINANCE_TESTNET_BASE_URL,
    FUTURES_EXCHANGE_INFO_PATH,
    InstrumentRegistry,
    fetch_instruments,
)
from aegis_observability import get_logger

__all__ = ["REGISTRY", "exchange_base_url", "prime"]

#: One cache per process. Read by the Jinja filters, written once at startup.
REGISTRY = InstrumentRegistry()


def exchange_base_url(settings: Settings) -> str:
    """REST host for public metadata: the futures host when that market is active."""
    if settings.execution.market_type.value == "futures":
        return settings.exchange.futures_base_url.rstrip("/")
    configured = settings.exchange.base_url.rstrip("/")
    if settings.exchange.testnet:
        return configured if "testnet.binance.vision" in configured else BINANCE_TESTNET_BASE_URL
    if "testnet.binance.vision" in configured:
        return BINANCE_MAINNET_BASE_URL
    return configured


def prime(settings: Settings) -> bool:
    """Load the venue's precision map. Returns ``True`` when the cache was filled."""
    logger = get_logger("aegis.api")
    futures = settings.execution.market_type.value == "futures"
    try:
        specs = fetch_instruments(
            exchange_base_url(settings),
            # Futures ticks differ from spot (BTCUSDT 0.10 vs 0.01), so use the right map.
            path=FUTURES_EXCHANGE_INFO_PATH if futures else "/api/v3/exchangeInfo",
            timeout_seconds=min(settings.market_data.request_timeout_seconds, 10.0),
        )
    except Exception as exc:  # network, HTTP status, malformed payload
        logger.warning(
            "instrument_precision_unavailable",
            error=str(exc),
            detail="falling back to default decimals for the dashboard",
        )
        return False
    REGISTRY.replace(specs)
    logger.info(
        "instrument_precision_loaded",
        symbols=len(specs),
        base_url=exchange_base_url(settings),
    )
    return True
