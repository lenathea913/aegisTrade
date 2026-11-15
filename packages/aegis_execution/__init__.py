"""AegisTrade execution.

``OrderIntent -> RiskGate -> ApprovedOrder -> ExecutionGateway -> broker``, with the
paper broker as the only implemented venue and ``LiveBroker`` a stub that refuses
everything. ``TradingPipeline`` is the single composition of gate, gateway and
ledger, so there is one code path to audit and one to test.
"""

from __future__ import annotations

from aegis_execution.broker import Broker, BrokerFill, LiveBroker, PaperBroker, build_broker
from aegis_execution.errors import ExecutionError, LiveTradingDisabledError
from aegis_execution.gateway import ExecutionGateway, ExecutionResult
from aegis_execution.pipeline import PipelineResult, TradingPipeline

__all__ = [
    "Broker",
    "BrokerFill",
    "ExecutionError",
    "ExecutionGateway",
    "ExecutionResult",
    "LiveBroker",
    "LiveTradingDisabledError",
    "PaperBroker",
    "PipelineResult",
    "TradingPipeline",
    "build_broker",
]
