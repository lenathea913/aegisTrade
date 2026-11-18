"""AegisTrade execution.

``OrderIntent -> RiskGate -> ApprovedOrder -> ExecutionGateway -> broker``, with the
paper broker as the default venue, ``BinanceTestnetBroker`` for spot and
``BinanceFuturesTestnetBroker`` for USDT-M (both testnet only) and ``LiveBroker`` a stub
that refuses everything.
``TradingPipeline`` is the single composition of gate, gateway and ledger, so there is
one code path to audit and one to test.
"""

from __future__ import annotations

from aegis_execution.binance_broker import (
    BinanceTestnetBroker,
    ExchangeOrderState,
    ensure_testnet_base_url,
    from_exchange_symbol,
)
from aegis_execution.binance_futures_broker import (
    BinanceFuturesTestnetBroker,
    FuturesPosition,
    ProtectionFill,
    ProtectionOrders,
    ensure_futures_testnet_base_url,
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
    "BinanceFuturesTestnetBroker",
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
    "FuturesPosition",
    "LiveBroker",
    "LiveTradingDisabledError",
    "NonTestnetEndpointError",
    "PaperBroker",
    "PipelineResult",
    "ProtectionFill",
    "ProtectionOrders",
    "TradingPipeline",
    "build_broker",
    "ensure_futures_testnet_base_url",
    "ensure_testnet_base_url",
    "from_exchange_symbol",
]
