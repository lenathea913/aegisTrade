"""Binance Spot Testnet broker.

The first adapter that talks to a real exchange API — and only to a testnet one.
Three rules shape it:

1. **Testnet only, provably.** ``ensure_testnet_base_url`` accepts a host under
   ``testnet.binance.vision`` and refuses everything else unless the operator set
   ``AEGIS_LIVE_CONFIRM=true``. A typo in a URL therefore cannot send paper money to
   the real book.
2. **Secrets never leave memory.** Keys arrive as ``SecretStr``, are read only inside
   :meth:`_signed_params`, and never appear in an exception message, a log record or a
   database row.
3. **Errors are typed.** A timeout, a 401 and a rejected order are different failures
   (``ExchangeUnavailableError``, ``ExchangeAuthError``, ``ExchangeError``) so the
   pipeline and the reconciler can react without parsing strings.

The adapter implements the ``Broker`` protocol (``submit``) plus the two calls the
reconciler needs (``fetch``, ``cancel``) and a cheap ``ping`` for the dashboard badge.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from pydantic import SecretStr

from aegis_core import Order, OrderStatus, OrderType, Side, utc_now
from aegis_execution.broker import BrokerFill
from aegis_execution.errors import (
    ExchangeAuthError,
    ExchangeError,
    ExchangeUnavailableError,
    NonTestnetEndpointError,
)

__all__ = [
    "DEFAULT_TESTNET_BASE_URL",
    "BinanceTestnetBroker",
    "ExchangeOrderState",
    "ensure_testnet_base_url",
    "from_exchange_symbol",
    "to_exchange_symbol",
]

DEFAULT_TESTNET_BASE_URL = "https://testnet.binance.vision"
_TESTNET_SUFFIX = "testnet.binance.vision"
_STATUS_MAP: dict[str, OrderStatus] = {
    "NEW": OrderStatus.NEW,
    "PENDING_CANCEL": OrderStatus.SUBMITTED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}
#: Binance answers with these when the key/secret/signature is wrong.
_AUTH_STATUS = frozenset({401, 403})


def to_exchange_symbol(symbol: str) -> str:
    """``BTC/USDT`` -> ``BTCUSDT`` (Binance's symbol format)."""
    return symbol.replace("/", "").upper()


def from_exchange_symbol(symbol: str) -> str:
    """``BTCUSDT`` -> ``BTC/USDT``; unknown quotes are returned unchanged."""
    upper = symbol.upper()
    for quote in ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH"):
        if upper.endswith(quote) and len(upper) > len(quote):
            return f"{upper[: -len(quote)]}/{quote}"
    return symbol


def ensure_testnet_base_url(base_url: str, *, live_confirm: bool = False) -> str:
    """Return ``base_url`` only when it is a testnet endpoint (or explicitly allowed).

    Raises:
        NonTestnetEndpointError: the host is not a Binance testnet and the operator
            did not set ``AEGIS_LIVE_CONFIRM=true``.
    """
    host = (urlparse(base_url).hostname or "").lower()
    # Only ``testnet.binance.vision`` and its subdomains. A prefix check would accept
    # lookalikes such as ``testnet.evil.com``, so compare the full host suffix.
    if host == _TESTNET_SUFFIX or host.endswith(f".{_TESTNET_SUFFIX}"):
        return base_url
    if live_confirm:
        return base_url
    raise NonTestnetEndpointError(
        f"refusing a non-testnet endpoint ({host or base_url!r}): this adapter is for "
        "Binance Spot Testnet. Set AEGIS_LIVE_CONFIRM=true only if you have decided to "
        "trade real money deliberately."
    )


@dataclass(frozen=True)
class ExchangeOrderState:
    """What the exchange says about one order right now."""

    exchange_order_id: str
    status: OrderStatus
    filled_qty: Decimal
    avg_price: Decimal | None
    #: Populated by ``fetch_open_orders``; the single-order calls leave them unset.
    symbol: str | None = None
    side: Side | None = None
    price: Decimal | None = None
    qty: Decimal | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


class BinanceTestnetBroker:
    """HMAC-signed Spot Testnet client implementing ``Broker``."""

    name = "binance-testnet"
    live = True

    def __init__(
        self,
        *,
        api_key: SecretStr | str,
        api_secret: SecretStr | str,
        base_url: str = DEFAULT_TESTNET_BASE_URL,
        live_confirm: bool = False,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
        recv_window: int = 5000,
    ) -> None:
        self._api_key = _value(api_key)
        self._api_secret = _value(api_secret)
        if not self._api_key or not self._api_secret:
            raise ExchangeAuthError(
                "exchange credentials are missing: set AEGIS_EXCHANGE__API_KEY and "
                "AEGIS_EXCHANGE__API_SECRET (testnet keys)"
            )
        self._base_url = ensure_testnet_base_url(base_url, live_confirm=live_confirm)
        # An injected client stays the caller's to close; one created here is ours.
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=self._base_url, timeout=timeout_seconds)
        self._timeout = timeout_seconds
        self._recv_window = recv_window

    # ------------------------------------------------------------------ broker

    def submit(
        self, order: Order, *, market_price: Decimal, now: datetime | None = None
    ) -> BrokerFill | None:
        """Place ``order``; return a fill when the exchange already executed part of it.

        ``None`` means "accepted but not filled yet" — the reconciler owns what happens
        next, which is exactly why partial fills are not guessed here.
        """
        params: dict[str, str] = {
            "symbol": to_exchange_symbol(order.symbol),
            "side": order.side.value,
            "type": order.order_type.value,
            "quantity": _decimal_text(order.qty),
            "newClientOrderId": order.order_id,
            "newOrderRespType": "FULL",
        }
        if order.order_type is OrderType.LIMIT:
            if order.price is None:  # pragma: no cover - domain validates this
                raise ExchangeError("LIMIT order reached the broker without a price")
            params["timeInForce"] = "GTC"
            params["price"] = _decimal_text(order.price)

        payload = _as_object(self._request("POST", "/api/v3/order", params), "/api/v3/order")
        state = _state_from_payload(payload, fallback_id=order.order_id)
        if state.filled_qty <= 0:
            return None
        return BrokerFill(
            exchange_order_id=state.exchange_order_id,
            qty=state.filled_qty,
            price=state.avg_price or market_price,
            fee=_commission(payload),
            filled_at=now if now is not None else utc_now(),
        )

    # ------------------------------------------------------------- reconciler

    def fetch(self, exchange_order_id: str, *, symbol: str) -> ExchangeOrderState:
        """Ask the exchange for one order's current state."""
        payload = _as_object(
            self._request(
                "GET",
                "/api/v3/order",
                {
                    "symbol": to_exchange_symbol(symbol),
                    "orderId": exchange_order_id,
                },
            ),
            "/api/v3/order",
        )
        return _state_from_payload(payload, fallback_id=exchange_order_id)

    def fetch_open_orders(self) -> tuple[ExchangeOrderState, ...]:
        """Every order the venue still has open (``GET /api/v3/openOrders``)."""
        payload = self._request("GET", "/api/v3/openOrders", {})
        if not isinstance(payload, list):
            raise ExchangeError("exchange returned an unexpected open-orders payload")
        return tuple(
            _state_from_payload(item, fallback_id="", with_order_fields=True)
            for item in payload
            if isinstance(item, dict)
        )

    def verify_credentials(self) -> None:
        """Signed ``GET /api/v3/account``; raises when the keys are invalid."""
        self._request("GET", "/api/v3/account", {})

    def cancel(self, exchange_order_id: str, *, symbol: str) -> ExchangeOrderState:
        """Cancel the remainder of an order and return its resulting state."""
        payload = _as_object(
            self._request(
                "DELETE",
                "/api/v3/order",
                {
                    "symbol": to_exchange_symbol(symbol),
                    "orderId": exchange_order_id,
                },
            ),
            "/api/v3/order",
        )
        return _state_from_payload(payload, fallback_id=exchange_order_id)

    def close(self) -> None:
        """Close the HTTP client, but only when this broker created it."""
        if self._owns_client:
            self._client.close()

    def ping(self) -> bool:
        """Cheap public probe used by the dashboard badge; never raises."""
        try:
            response = self._client.get("/api/v3/time", timeout=min(self._timeout, 3.0))
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    # --------------------------------------------------------------- internals

    def _signed_params(self, params: dict[str, str]) -> dict[str, str]:
        """Attach timestamp, recvWindow and an HMAC-SHA256 signature."""
        timestamp = str(int(utc_now().timestamp() * 1000))
        signed = {**params, "recvWindow": str(self._recv_window), "timestamp": timestamp}
        query = urlencode(signed)
        signature = hmac.new(
            self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {**signed, "signature": signature}

    def _request(
        self, method: str, path: str, params: dict[str, str]
    ) -> dict[str, Any] | list[Any]:
        signed = self._signed_params(params)
        headers = {"X-MBX-APIKEY": self._api_key}
        try:
            response = self._client.request(
                method, path, params=signed, headers=headers, timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise ExchangeUnavailableError(f"exchange timed out on {path}") from exc
        except httpx.HTTPError as exc:
            raise ExchangeUnavailableError(f"exchange unreachable on {path}: {exc}") from exc

        if response.status_code in _AUTH_STATUS:
            raise ExchangeAuthError(
                f"exchange rejected the credentials on {path} (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            detail = _error_message(response)
            raise ExchangeError(f"exchange error on {path} (HTTP {response.status_code}): {detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExchangeError(f"exchange returned non-JSON on {path}") from exc
        if not isinstance(payload, (dict, list)):
            raise ExchangeError(f"exchange returned an unexpected payload on {path}")
        return payload


def _as_object(payload: dict[str, Any] | list[Any], path: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ExchangeError(f"exchange returned an unexpected payload on {path}")
    return payload


def _decimal_or_none(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return None
    return value if value > 0 else None


def _value(secret: SecretStr | str) -> str:
    return secret.get_secret_value() if isinstance(secret, SecretStr) else secret


def _decimal_text(value: Decimal) -> str:
    """Plain decimal text, never scientific notation (Binance rejects ``1E-8``)."""
    return format(value, "f")


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        return str(payload.get("msg", payload))[:200]
    return str(payload)[:200]


def _commission(payload: dict[str, Any]) -> Decimal:
    """Sum of commissions reported for the fills (asset-agnostic, often the quote)."""
    fills = payload.get("fills")
    if not isinstance(fills, list):
        return Decimal("0")
    total = Decimal("0")
    for item in fills:
        if isinstance(item, dict) and item.get("commission") is not None:
            total += Decimal(str(item["commission"]))
    return total


def _state_from_payload(
    payload: dict[str, Any], *, fallback_id: str, with_order_fields: bool = False
) -> ExchangeOrderState:
    raw_status = str(payload.get("status", "")).upper()
    status = _STATUS_MAP.get(raw_status)
    if status is None:
        raise ExchangeError(f"exchange returned an unknown order status: {raw_status!r}")
    filled = Decimal(str(payload.get("executedQty", "0") or "0"))
    avg_price: Decimal | None = None
    if filled > 0:
        quote_qty = payload.get("cummulativeQuoteQty")
        if quote_qty is not None and Decimal(str(quote_qty)) > 0:
            avg_price = Decimal(str(quote_qty)) / filled
        else:
            avg_price = Decimal(str(payload.get("price", "0") or "0")) or None
    raw_side = str(payload.get("side", "")).upper()
    return ExchangeOrderState(
        exchange_order_id=str(payload.get("orderId", fallback_id)),
        status=status,
        filled_qty=filled,
        avg_price=avg_price,
        symbol=from_exchange_symbol(str(payload.get("symbol", ""))) if with_order_fields else None,
        side=Side(raw_side) if with_order_fields and raw_side in {"BUY", "SELL"} else None,
        price=_decimal_or_none(payload.get("price")) if with_order_fields else None,
        qty=_decimal_or_none(payload.get("origQty")) if with_order_fields else None,
    )
