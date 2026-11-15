"""AegisTrade portfolio ledger.

The ledger is the source of truth for exposure and equity: positions, realized PnL
history (with fee/funding effects), marks, and a frozen ``PortfolioState`` snapshot
for the risk engine. Persistence and audit rows live here too, but the arithmetic
itself is pure.
"""

from __future__ import annotations

from aegis_ledger.audit import AuditEvent, EventType, InMemoryJournal, Journal
from aegis_ledger.errors import LedgerError
from aegis_ledger.ledger import (
    LedgerUpdate,
    PortfolioLedger,
    PositionProtection,
    RealizedEvent,
    ReconstructionSummary,
)
from aegis_ledger.records import (
    audit_row,
    order_row,
    position_from_row,
    position_row,
    trade_from_row,
    trade_row,
)
from aegis_ledger.store import LedgerStore, LedgerWriter, build_position_upsert

__all__ = [
    "AuditEvent",
    "EventType",
    "InMemoryJournal",
    "Journal",
    "LedgerError",
    "LedgerStore",
    "LedgerUpdate",
    "LedgerWriter",
    "PortfolioLedger",
    "PositionProtection",
    "RealizedEvent",
    "ReconstructionSummary",
    "audit_row",
    "build_position_upsert",
    "order_row",
    "position_from_row",
    "position_row",
    "trade_from_row",
    "trade_row",
]
