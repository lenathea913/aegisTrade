"""Binance USDT-M Futures Testnet broker.

The spot adapter talks to ``testnet.binance.vision``; futures live on a different host
and a different API surface (``/fapi``). This adapter is deliberately narrower than the
spot one in one respect: it **only** accepts the futures testnet host. Futures is not
enabled for mainnet until the operator has verified it by hand, so there is no
``live_confirm`` escape hatch here — a production host is refused outright.

Three rules shape it:

1. **Isolated margin, always.** ``set_margin_type(ISOLATED)`` runs before the first
   order for a symbol; cross margin is never requested. Binance answers ``-4046`` when
   the mode already matches, which is treated as success.
2. **Leverage is set explicitly, per symbol.** The venue keeps leverage as symbol state,
   so the adapter calls ``/fapi/v1/leverage`` whenever the requested value differs from
   what it last sent (cached per symbol/leverage pair).
3. **Hedge mode is addressed by leg.** ``positionSide`` comes from the order: closing a
   long is a SELL on the ``LONG`` leg, which is not derivable from the side alone.
   ``reduceOnly`` is not sent — Binance rejects it in hedge mode.
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

from aegis_core import MAX_LEVERAGE, Order, OrderStatus, OrderType, PositionSide, Side, utc_now
from aegis_execution.binance_broker import (
    ExchangeOrderState,
    _as_object,
    _decimal_text,
    _error_message,
    _value,
    from_exchange_symbol,
    to_exchange_symbol,
)
from aegis_execution.broker import BrokerFill
from aegis_execution.errors import (
    ExchangeAuthError,
    ExchangeError,
    ExchangeUnavailableError,
    NonTestnetEndpointError,
)

__all__ = [
    "DEFAULT_FUTURES_TESTNET_BASE_URL",
    "BinanceFuturesTestnetBroker",
    "FuturesPosition",
    "ensure_futures_testnet_base_url",
]

DEFAULT_FUTURES_TESTNET_BASE_URL = "https://testnet.binancefuture.com"
_FUTURES_PREFIX = "/fapi"
_TESTNET_HOSTS = frozenset({"testnet.binancefuture.com"})
_AUTH_STATUS = frozenset({401, 403})
#: "No need to change margin type" — already ISOLATED.
_MARGIN_ALREADY_SET = -4046
_STATUS_MAP: dict[str, OrderStatus] = {
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}


def ensure_futures_testnet_base_url(base_url: str) -> str:
    """Return ``base_url`` only for the Binance Futures Testnet host.

    Unlike the spot adapter there is no opt-out: futures has not been verified against
    a real account, so mainnet is refused even when ``AEGIS_LIVE_CONFIRM`` is set.
    """
    host = (urlparse(base_url).hostname or "").lower()
    if host in _TESTNET_HOSTS:
        return base_url.rstrip("/")
    raise NonTestnetEndpointError(
        f"refusing futures endpoint {host or base_url!r}: USDT-M futures is testnet-only "
        "for now (https://testnet.binancefuture.com). Mainnet futures is deliberately "
        "not implemented."
    )


@dataclass(frozen=True)
class FuturesPosition:
    """One leg of one symbol as the venue reports it."""

    symbol: str
    position_side: PositionSide
    qty: Decimal
    entry_price: Decimal
    mark_price: Decimal | None
    unrealized_pnl: Decimal
    leverage: int
    liquidation_price: Decimal | None
    isolated_margin: Decimal | None


class BinanceFuturesTestnetBroker:
    """HMAC-signed USDT-M Futures Testnet client (``submit`` + reconciler calls)."""

    name = "binance-futures-testnet"
    live = True

    def __init__(
        self,
        *,
        api_key: SecretStr | str,
        api_secret: SecretStr | str,
        base_url: str = DEFAULT_FUTURES_TESTNET_BASE_URL,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
        recv_window: int = 5000,
        margin_type: str = "ISOLATED",
    ) -> None:
        if margin_type != "ISOLATED":
            raise ValueError("futures margin mode must be ISOLATED; cross is not supported")
        self._api_key = _value(api_key)
        self._api_secret = _value(api_secret)
        if not self._api_key or not self._api_secret:
            raise ExchangeAuthError(
                "futures credentials are missing: set AEGIS_EXCHANGE__API_KEY and "
                "AEGIS_EXCHANGE__API_SECRET (futures testnet keys)"
            )
        self._base_url = ensure_futures_testnet_base_url(base_url)
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=self._base_url, timeout=timeout_seconds)
        self._timeout = timeout_seconds
        self._recv_window = recv_window
        self._margin_type = margin_type
        #: symbol -> the leverage last sent to the venue (leverage is venue state).
        self._leverage_sent: dict[str, int] = {}
        self._margin_set: set[str] = set()

    # ------------------------------------------------------------- preparation

    def set_margin_type(self, symbol: str) -> None:
        """Ask for isolated margin; a venue-side no-op (``-4046``) is success."""
        venue_symbol = to_exchange_symbol(symbol)
        if venue_symbol in self._margin_set:
            return
        try:
            self._request(
                "POST", "/v1/marginType", {"symbol": venue_symbol, "marginType": "ISOLATED"}
            )
        except ExchangeError as exc:
            if _MARGIN_ALREADY_SET not in _error_code(str(exc)):
                raise
        self._margin_set.add(venue_symbol)

    def set_leverage(self, symbol: str, leverage: int) -> None:
        """Send leverage for ``symbol`` when it differs from what was last sent."""
        if leverage < 1 or leverage > MAX_LEVERAGE:
            raise ExchangeError(f"leverage {leverage} is outside 1..{MAX_LEVERAGE}")
        venue_symbol = to_exchange_symbol(symbol)
        if self._leverage_sent.get(venue_symbol) == leverage:
            return
        self._request("POST", "/v1/leverage", {"symbol": venue_symbol, "leverage": str(leverage)})
        self._leverage_sent[venue_symbol] = leverage

    def _prepare(self, order: Order) -> None:
        """Margin mode first, then leverage: both before any order is sent."""
        self.set_margin_type(order.symbol)
        self.set_leverage(order.symbol, order.leverage)

    # ------------------------------------------------------------------ broker

    def submit(
        self,
        order: Order,
        *,
        market_price: Decimal,
        now: datetime | None = None,
    ) -> BrokerFill | None:
        """Prepare the symbol, place ``order``, and report a fill when already executed."""
        self._prepare(order)
        position_side = order.position_side or PositionSide.for_entry(order.side)
        params: dict[str, str] = {
            "symbol": to_exchange_symbol(order.symbol),
            "side": order.side.value,
            "positionSide": position_side.value,
            "type": order.order_type.value,
            "quantity": _decimal_text(order.qty),
            "newClientOrderId": order.order_id,
            "newOrderRespType": "RESULT",
        }
        if order.order_type is OrderType.LIMIT:
            if order.price is None:  # pragma: no cover - domain validates this
                raise ExchangeError("LIMIT order reached the broker without a price")
            params["timeInForce"] = "GTC"
            params["price"] = _decimal_text(order.price)

        payload = _as_object(self._request("POST", "/v1/order", params), "/fapi/v1/order")
        state = _state_from_payload(payload, fallback_id=order.order_id)
        if state.filled_qty <= 0:
            return None
        return BrokerFill(
            exchange_order_id=state.exchange_order_id,
            qty=state.filled_qty,
            price=state.avg_price or market_price,
            fee=Decimal("0"),  # futures commissions arrive in the userTrades stream
            filled_at=now if now is not None else utc_now(),
        )

    # ------------------------------------------------------------- reconciler

    def fetch(self, exchange_order_id: str, *, symbol: str) -> ExchangeOrderState:
        payload = _as_object(
            self._request(
                "GET",
                "/v1/order",
                {"symbol": to_exchange_symbol(symbol), "orderId": exchange_order_id},
            ),
            "/fapi/v1/order",
        )
        return _state_from_payload(payload, fallback_id=exchange_order_id)

    def fetch_open_orders(self) -> tuple[ExchangeOrderState, ...]:
        payload = self._request("GET", "/v1/openOrders", {})
        if not isinstance(payload, list):
            raise ExchangeError("exchange returned an unexpected open-orders payload")
        return tuple(
            _state_from_payload(item, fallback_id="", with_order_fields=True)
            for item in payload
            if isinstance(item, dict)
        )

    def fetch_positions(self) -> tuple[FuturesPosition, ...]:
        """Every open leg (``GET /fapi/v2/positionRisk``)."""
        payload = self._request("GET", "/v2/positionRisk", {})
        if not isinstance(payload, list):
            raise ExchangeError("exchange returned an unexpected position payload")
        positions: list[FuturesPosition] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            qty = _decimal(item.get("positionAmt"))
            if qty == 0:
                continue
            raw_side = str(item.get("positionSide", "")).upper()
            side = (
                PositionSide(raw_side)
                if raw_side in {"LONG", "SHORT"}
                else (PositionSide.LONG if qty > 0 else PositionSide.SHORT)
            )
            positions.append(
                FuturesPosition(
                    symbol=from_exchange_symbol(str(item.get("symbol", ""))),
                    position_side=side,
                    qty=abs(qty),
                    entry_price=_decimal(item.get("entryPrice")),
                    mark_price=_decimal_or_none(item.get("markPrice")),
                    unrealized_pnl=_decimal(item.get("unRealizedProfit")),
                    leverage=int(_decimal(item.get("leverage")) or 1),
                    liquidation_price=_decimal_or_none(item.get("liquidationPrice")),
                    isolated_margin=_decimal_or_none(item.get("isolatedMargin")),
                )
            )
        return tuple(positions)

    def cancel(self, exchange_order_id: str, *, symbol: str) -> ExchangeOrderState:
        payload = _as_object(
            self._request(
                "DELETE",
                "/v1/order",
                {"symbol": to_exchange_symbol(symbol), "orderId": exchange_order_id},
            ),
            "/fapi/v1/order",
        )
        return _state_from_payload(payload, fallback_id=exchange_order_id)

    def verify_credentials(self) -> None:
        self._request("GET", "/v2/account", {})

    def ping(self) -> bool:
        try:
            response = self._client.get(
                f"{self._base_url}{_FUTURES_PREFIX}/v1/time", timeout=min(self._timeout, 3.0)
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # --------------------------------------------------------------- internals

    def _signed_params(self, params: dict[str, str]) -> dict[str, str]:
        timestamp = str(int(utc_now().timestamp() * 1000))
        signed = {**params, "recvWindow": str(self._recv_window), "timestamp": timestamp}
        query = urlencode(signed)
        signature = hmac.new(
            self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {**signed, "signature": signature}

    def _request(self, method: str, path: str, params: dict[str, str]) -> Any:
        # Absolute URL: the caller may inject a client with or without a base_url.
        url = f"{self._base_url}{_FUTURES_PREFIX}{path}"
        signed = self._signed_params(params)
        headers = {"X-MBX-APIKEY": self._api_key}
        try:
            response = self._client.request(
                method, url, params=signed, headers=headers, timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise ExchangeUnavailableError(f"exchange timed out on {url}") from exc
        except httpx.HTTPError as exc:
            raise ExchangeUnavailableError(f"exchange unreachable on {url}: {exc}") from exc

        if response.status_code in _AUTH_STATUS:
            raise ExchangeAuthError(
                f"exchange rejected the credentials on {url} (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            # Keep the numeric code: some errors below are matched on it (-4046).
            raise ExchangeError(
                f"exchange error on {url} (HTTP {response.status_code}): {_error_detail(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExchangeError(f"exchange returned non-JSON on {url}") from exc
        if not isinstance(payload, (dict, list)):
            raise ExchangeError(f"exchange returned an unexpected payload on {url}")
        return payload


def _error_detail(response: httpx.Response) -> str:
    """``[code] msg`` when the body is Binance's error object, the text otherwise."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict) and "code" in payload:
        return f"[{payload.get('code')}] {str(payload.get('msg', '')).strip()}"
    return _error_message(response)


def _error_code(message: str) -> set[int]:
    """Every integer that looks like a Binance error code in ``message``."""
    codes: set[int] = set()
    cleaned = message.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ")
    for token in cleaned.replace(":", " ").split():
        if token.lstrip("-").isdigit():
            codes.add(int(token))
    return codes


def _decimal(raw: Any) -> Decimal:
    if raw is None or raw == "":
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return Decimal("0")


def _decimal_or_none(raw: Any) -> Decimal | None:
    value = _decimal(raw)
    return value if value > 0 else None


def _state_from_payload(
    payload: dict[str, Any], *, fallback_id: str, with_order_fields: bool = False
) -> ExchangeOrderState:
    raw_status = str(payload.get("status", "")).upper()
    status = _STATUS_MAP.get(raw_status)
    if status is None:
        raise ExchangeError(f"exchange returned an unknown order status: {raw_status!r}")
    filled = _decimal(payload.get("executedQty"))
    avg_price: Decimal | None = None
    if filled > 0:
        quote_qty = _decimal(payload.get("cumQuote")) or _decimal(payload.get("cumQuoteQty"))
        if quote_qty > 0:
            avg_price = quote_qty / filled
        else:
            avg_price = _decimal_or_none(payload.get("avgPrice"))
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
