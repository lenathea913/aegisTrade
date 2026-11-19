"""Does the venue still hold the leg the monitor is about to close?

The monitor and the venue's ``STOP_MARKET``/``TAKE_PROFIT_MARKET`` orders are two
independent executors for the same exposure. When the stop fires first, the book only
learns about it on the next reconciliation pass, and a monitor running in between would
send its own closing market order for a quantity the venue no longer holds. That order is
not harmless: hedge-mode orders are sent without ``reduceOnly`` (Binance rejects it
there), so an oversized close is not merely trimmed — it can be rejected while the book
keeps an exposure the venue has already dropped.

So before an exit leaves the process, the monitor asks the venue one question: *how much
of this leg do you still hold?* An exit is only withheld when the answer is unambiguous —
the venue holds less than the book — because an unknown answer must not be turned into
"the venue handled it".

Who closed the leg does not matter. If the venue holds less than the book, the book is
stale and every order sized from it is wrong; booking the fill and re-arming the
remainder is the reconciler's job, not the monitor's.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from aegis_core import PositionSide, Side

__all__ = ["BrokerVenueCheck", "VenueCheck"]

_ZERO = Decimal("0")


class VenueCheck(Protocol):
    """The pre-exit question, small enough to fake in a test."""

    def held_qty(self, symbol: str, *, side: Side) -> Decimal:
        """Quantity the venue still holds on the leg that ``side`` opened (unsigned)."""


class BrokerVenueCheck:
    """Reads the leg's quantity from a futures broker (``GET /fapi/v2/positionRisk``)."""

    def __init__(self, broker: Any) -> None:
        self._broker = broker

    def held_qty(self, symbol: str, *, side: Side) -> Decimal:
        wanted = PositionSide.for_entry(side).value
        return sum(
            (
                abs(leg.qty)
                for leg in self._broker.fetch_positions()
                if leg.symbol == symbol
                and str(getattr(leg.position_side, "value", leg.position_side)).upper() == wanted
            ),
            _ZERO,
        )
