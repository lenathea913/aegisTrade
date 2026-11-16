"""AegisTrade execution.

``OrderIntent -> RiskGate -> ApprovedOrder -> ExecutionGateway -> broker``, with the
paper broker as the default venue, ``BinanceTestnetBroker`` as the first real exchange
adapter (testnet only) and ``LiveBroker`` a stub that refuses everything.
``TradingPipeline`` is the single composition of gate, gateway and ledger, so there is
one code path to audit and one to test.
"""

from __future__ import annotations

from aegis_execution.binance_broker import (
    BinanceTestnetBroker,
    ExchangeOrderState,
    ensure_testnet_base_url,
)
from aegis_execution.broker import Broker, BrokerFill, LiveBroker, PaperBroker, build_broker
from aegis_execution.errors import (
    ExchangeAuthError,
    ExchangeError,
    ExchangeUnavailableError,
    ExecutionError,
    LiveTradingDisabledError,
    NonTestnetEndpointError,
)
from aegis_execution.gateway import ExecutionGateway, ExecutionResult
from aegis_execution.pipeline import PipelineResult, TradingPipeline

__all__ = [
    "BinanceTestnetBroker",
    "Broker",
    "BrokerFill",
    "ExchangeAuthError",
    "ExchangeError",
    "ExchangeOrderState",
    "ExchangeUnavailableError",
    "ExecutionError",
    "ExecutionGateway",
    "ExecutionResult",
    "LiveBroker",
    "LiveTradingDisabledError",
    "NonTestnetEndpointError",
    "PaperBroker",
    "PipelineResult",
    "TradingPipeline",
    "build_broker",
    "ensure_testnet_base_url",
]
