"""Realistic single-symbol backtest engine.

    run(strategy, data, config) -> BacktestResult

Costs are explicit: taker/maker fees, slippage, funding, optional partial fills and
simulated latency. Signals are computed only from closed candles and always fill on
a later bar, so the result cannot benefit from look-ahead.
"""

from __future__ import annotations

from aegis_backtest.config import BacktestConfig
from aegis_backtest.data import TIMEFRAME_DELTAS, load_candles_csv
from aegis_backtest.engine import run
from aegis_backtest.models import (
    BacktestResult,
    BacktestTrade,
    EquityPoint,
    ExitReason,
    RejectedIntent,
)

__all__ = [
    "TIMEFRAME_DELTAS",
    "BacktestConfig",
    "BacktestResult",
    "BacktestTrade",
    "EquityPoint",
    "ExitReason",
    "RejectedIntent",
    "load_candles_csv",
    "run",
]
