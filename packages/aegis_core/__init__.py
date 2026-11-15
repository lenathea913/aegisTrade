"""AegisTrade domain model.

Pure, dependency-free (except pydantic) value objects shared by every service:

* ``OrderIntent`` -> ``ApprovedOrder`` -> ``Order`` is the only path to execution.
* ``Position`` and ``Trade`` describe exposure and realized results.
* ``RiskLimit`` / ``RiskDecision`` describe the gate's configuration and verdict.
* All money is ``Decimal``, all times are aware UTC.
"""

from __future__ import annotations

from aegis_core.base import StrictModel
from aegis_core.candle import Candle
from aegis_core.clock import to_utc, utc_now
from aegis_core.enums import OrderStatus, OrderType, Side, Timeframe
from aegis_core.errors import (
    AegisError,
    DomainError,
    InvalidStateTransitionError,
    UnapprovedOrderError,
)
from aegis_core.ids import new_id
from aegis_core.order import ApprovedOrder, Order, OrderIntent
from aegis_core.position import Position
from aegis_core.risk import RiskDecision, RiskLimit
from aegis_core.state import MarketContext, MarketQuote, PortfolioState
from aegis_core.trade import Trade

__version__ = "0.1.0"

__all__ = [
    "AegisError",
    "ApprovedOrder",
    "Candle",
    "DomainError",
    "InvalidStateTransitionError",
    "MarketContext",
    "MarketQuote",
    "Order",
    "OrderIntent",
    "OrderStatus",
    "OrderType",
    "PortfolioState",
    "Position",
    "RiskDecision",
    "RiskLimit",
    "Side",
    "StrictModel",
    "Timeframe",
    "Trade",
    "UnapprovedOrderError",
    "__version__",
    "new_id",
    "to_utc",
    "utc_now",
]
