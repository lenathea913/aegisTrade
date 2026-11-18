"""Position monitor: the exit side of risk management.

The risk gate governs entries. This worker governs exits: it watches every open
position that carries protection levels, compares the latest price with the recorded
stop and target, and sends a closing ``OrderIntent`` through the *same* gate ->
gateway -> ledger pipeline as an entry. Nothing is closed behind the risk engine's
back; a protective exit is simply an intent the gate recognises as risk-reducing
(``docs/adr/0009-protective-exits-through-the-gate.md``).
"""

from __future__ import annotations

from position_monitor.monitor import ExitTrigger, MonitorAction, PositionMonitor
from position_monitor.quotes import CandleQuoteSource, QuoteSource
from position_monitor.venue import BrokerVenueCheck, VenueCheck, VenuePositionState

__all__ = [
    "BrokerVenueCheck",
    "CandleQuoteSource",
    "ExitTrigger",
    "MonitorAction",
    "PositionMonitor",
    "QuoteSource",
    "VenueCheck",
    "VenuePositionState",
]
