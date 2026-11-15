"""Brokers: where an ``Order`` turns into a fill (or nothing).

``PaperBroker`` simulates a fill from the current market price: slippage is applied
adversely, fees are charged in basis points, and a LIMIT order only fills when the
market has actually reached the limit. ``LiveBroker`` is an explicit stub — it
refuses every call, so no code path in the MVP can reach a real exchange.

Brokers never see ``OrderIntent`` or ``ApprovedOrder`` by type; they receive an
``Order`` that the gateway built from an approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from aegis_config import ExecutionSettings
from aegis_core import Order, OrderType, Side, utc_now
from aegis_execution.errors import LiveTradingDisabledError

__all__ = ["Broker", "BrokerFill", "LiveBroker", "PaperBroker", "build_broker"]

_BPS = Decimal("10000")
_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True)
class BrokerFill:
    """One broker-side fill."""

    exchange_order_id: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    filled_at: datetime


class Broker(Protocol):
    """What the gateway needs from any broker."""

    name: str

    def submit(
        self, order: Order, *, market_price: Decimal, now: datetime | None = None
    ) -> BrokerFill | None:
        """Send ``order``; return the fill, or ``None`` when it did not trade."""


class PaperBroker:
    """Deterministic simulator: no network, no credentials, no real money."""

    name = "paper"

    def __init__(
        self,
        *,
        fee_bps: Decimal = Decimal("5"),
        slippage_bps: Decimal = Decimal("5"),
    ) -> None:
        if fee_bps < 0 or slippage_bps < 0:
            raise ValueError("fee_bps and slippage_bps must not be negative")
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps

    @classmethod
    def from_settings(cls, settings: ExecutionSettings) -> PaperBroker:
        return cls(fee_bps=settings.fee_bps, slippage_bps=settings.slippage_bps)

    def _slipped(self, order: Order, market_price: Decimal) -> Decimal:
        """Adverse slippage: buys pay more, sells receive less."""
        adjustment = market_price * self.slippage_bps / _BPS
        price = market_price + adjustment if order.side is Side.BUY else market_price - adjustment
        return price.quantize(_QUANTUM)

    def submit(
        self, order: Order, *, market_price: Decimal, now: datetime | None = None
    ) -> BrokerFill | None:
        if market_price <= 0:
            raise ValueError("market_price must be greater than 0")
        filled_at = now if now is not None else utc_now()

        if order.order_type is OrderType.MARKET:
            price = self._slipped(order, market_price)
        else:
            # A limit only trades when the market reached it; the fill is at the limit.
            assert order.price is not None  # LIMIT validated by the domain
            touched = (
                market_price <= order.price
                if order.side is Side.BUY
                else market_price >= order.price
            )
            if not touched:
                return None
            price = order.price

        fee = (order.qty * price * self.fee_bps / _BPS).quantize(_QUANTUM)
        return BrokerFill(
            exchange_order_id=f"paper-{order.order_id}",
            qty=order.qty,
            price=price,
            fee=fee,
            filled_at=filled_at,
        )


class LiveBroker:
    """Stub for a real exchange adapter. It never trades in this MVP."""

    name = "live"

    def submit(
        self, order: Order, *, market_price: Decimal, now: datetime | None = None
    ) -> BrokerFill | None:
        raise LiveTradingDisabledError(
            "live broker is a stub: no exchange adapter ships with the MVP, "
            "so live orders are refused"
        )


def build_broker(settings: ExecutionSettings) -> Broker:
    """Broker selected by configuration (mode and broker are cross-validated)."""
    if settings.broker == "paper":
        return PaperBroker.from_settings(settings)
    if settings.broker == "live":
        return LiveBroker()
    raise ValueError(f"unknown broker: {settings.broker!r}")
