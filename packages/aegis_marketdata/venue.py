"""Fetching ``exchangeInfo`` from the venue.

Read-only public market metadata: no key, no account, nothing that can trade. A caller
that cannot reach the venue keeps its previous cache (or the defaults) and logs the
failure; precision is a display concern, never a reason to stop serving the dashboard.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from aegis_marketdata.instruments import InstrumentSpec, parse_exchange_info

__all__ = [
    "BINANCE_MAINNET_BASE_URL",
    "BINANCE_TESTNET_BASE_URL",
    "FUTURES_EXCHANGE_INFO_PATH",
    "SPOT_EXCHANGE_INFO_PATH",
    "fetch_instruments",
]

BINANCE_TESTNET_BASE_URL = "https://testnet.binance.vision"
BINANCE_MAINNET_BASE_URL = "https://api.binance.com"
SPOT_EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
#: USDT-M futures publish their filters on a different path; ticks differ from spot
#: (BTCUSDT quotes 0.10 on futures, 0.01 on spot), so the dashboard must use this one.
FUTURES_EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"


def fetch_instruments(
    base_url: str,
    *,
    path: str = SPOT_EXCHANGE_INFO_PATH,
    timeout_seconds: float = 10.0,
    client: httpx.Client | None = None,
) -> dict[str, InstrumentSpec]:
    """GET ``exchangeInfo`` (spot or futures) and return the parsed precision map."""
    url = f"{base_url.rstrip('/')}{path}"
    if client is not None:
        payload: Mapping[str, Any] = client.get(url).raise_for_status().json()
        return parse_exchange_info(payload)
    with httpx.Client(timeout=timeout_seconds) as owned:
        payload = owned.get(url).raise_for_status().json()
    return parse_exchange_info(payload)
