"""Reading and cancelling real exchange orders.

The dashboard never keeps a broker (and therefore an HTTP client) alive between
requests: each call builds one, uses it, and closes it in ``finally``. That keeps the
API free of long-lived credentials and sockets, and it means a cancelled order cannot be
sent through a stale connection.

Paper mode short-circuits: there is no venue, so the list is empty and no call is made.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from aegis_config import Settings
from aegis_execution import ExchangeError, ExchangeOrderState, build_broker

__all__ = ["OpenOrderView", "cancel_order", "open_orders"]

#: Injectable so tests can hand in a fake broker instead of an HTTP client.
BrokerFactory = Callable[..., Any]


@dataclass(frozen=True)
class OpenOrderView:
    """One order still live on the venue, in the shape the UI needs."""

    order_id: str
    symbol: str
    side: str | None
    status: str
    price: Decimal | None
    qty: Decimal | None
    filled_qty: Decimal


def _with_broker(settings: Settings, action: Callable[[Any], Any], factory: BrokerFactory) -> Any:
    broker = factory(
        settings.execution, exchange=settings.exchange, live_confirm=settings.live_confirm
    )
    try:
        return action(broker)
    finally:
        close = getattr(broker, "close", None)
        if callable(close):
            close()


def _view(state: ExchangeOrderState) -> OpenOrderView:
    return OpenOrderView(
        order_id=state.exchange_order_id,
        symbol=state.symbol or "",
        side=None if state.side is None else state.side.value,
        status=state.status.value,
        price=state.price,
        qty=state.qty,
        filled_qty=state.filled_qty,
    )


def open_orders(
    settings: Settings, *, factory: BrokerFactory = build_broker
) -> tuple[OpenOrderView, ...]:
    """Orders the venue still reports as open (empty for the paper broker)."""

    def action(broker: Any) -> tuple[OpenOrderView, ...]:
        if not getattr(broker, "live", False):
            return ()
        fetch = getattr(broker, "fetch_open_orders", None)
        if fetch is None:
            return ()
        return tuple(_view(state) for state in fetch())

    return cast(tuple[OpenOrderView, ...], _with_broker(settings, action, factory))


def cancel_order(
    settings: Settings,
    order_id: str,
    symbol: str,
    *,
    factory: BrokerFactory = build_broker,
) -> str:
    """Cancel one order and return the exchange's resulting status."""

    def action(broker: Any) -> str:
        cancel = getattr(broker, "cancel", None)
        if cancel is None:
            raise ExchangeError("this broker cannot cancel orders")
        state: ExchangeOrderState = cancel(order_id, symbol=symbol)
        return state.status.value

    return cast(str, _with_broker(settings, action, factory))
