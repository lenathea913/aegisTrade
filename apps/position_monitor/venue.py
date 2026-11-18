"""Does the venue still hold the leg the monitor is about to close?

The monitor and the venue's ``STOP_MARKET``/``TAKE_PROFIT_MARKET`` orders are two
independent executors for the same exposure. When the stop fires first, the book only
learns about it on the next reconciliation pass, and a monitor running in between would
send its own closing market order against a leg that no longer exists — at best a
rejected order, at worst a fresh position nobody asked for.

So before an exit leaves the process, the monitor asks the venue one question: *is the
recorded protection order still working, and do you still hold as much as we think?* An
exit is only withheld when the answer is unambiguous — the protection order is gone
**and** the venue holds less than the book — because an unknown answer must not be
turned into "the venue handled it".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from aegis_core import PositionSide, Side

__all__ = ["BrokerVenueCheck", "VenueCheck", "VenuePositionState"]

_ZERO = Decimal("0")


@dataclass(frozen=True)
class VenuePositionState:
    """What the venue reports for one symbol, at one moment."""

    #: Quantity the venue still holds on the leg being closed (unsigned).
    venue_qty: Decimal
    #: True when at least one recorded protection order is still working.
    protection_open: bool


class VenueCheck(Protocol):
    """The pre-exit question, small enough to fake in a test."""

    def inspect(
        self, symbol: str, *, side: Side, protection_ids: Sequence[str] = ()
    ) -> VenuePositionState:
        """Current venue exposure on ``side`` and whether a protection order is live."""


class BrokerVenueCheck:
    """Reads ``openOrders`` and ``positionRisk`` from a futures broker."""

    def __init__(self, broker: Any) -> None:
        self._broker = broker

    def inspect(
        self, symbol: str, *, side: Side, protection_ids: Sequence[str] = ()
    ) -> VenuePositionState:
        open_ids = {str(order_id) for order_id in self._broker.fetch_open_order_ids(symbol)}
        wanted = PositionSide.for_entry(side).value
        held = sum(
            (
                abs(leg.qty)
                for leg in self._broker.fetch_positions()
                if leg.symbol == symbol
                and str(getattr(leg.position_side, "value", leg.position_side)).upper() == wanted
            ),
            _ZERO,
        )
        return VenuePositionState(
            venue_qty=held,
            protection_open=any(order_id in open_ids for order_id in protection_ids),
        )
