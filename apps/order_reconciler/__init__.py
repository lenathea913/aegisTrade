"""Reconciliation with the exchange.

Two jobs, both read-only towards the venue:

* **orders** — records the outcomes the pipeline could not see (late fills, partial
  fills, cancellations). It never approves or creates an order.
* **positions** — compares the book with the venue's positions and halts entries when
  they disagree, because every risk decision after a silent divergence is computed
  against exposure that may not exist.
"""

from __future__ import annotations

from order_reconciler.positions import (
    PositionDrift,
    PositionReconciler,
    VenueLeg,
    VenuePositionFeed,
)
from order_reconciler.reconciler import (
    ExchangeOrderFeed,
    OpenOrder,
    OrderBook,
    OrderReconciler,
    ReconciliationResult,
)
from order_reconciler.repository import SqlOrderBook
from order_reconciler.state import ReconciliationSnapshot, SqlReconciliationState

__all__ = [
    "ExchangeOrderFeed",
    "OpenOrder",
    "OrderBook",
    "OrderReconciler",
    "PositionDrift",
    "PositionReconciler",
    "ReconciliationResult",
    "ReconciliationSnapshot",
    "SqlOrderBook",
    "SqlReconciliationState",
    "VenueLeg",
    "VenuePositionFeed",
]
