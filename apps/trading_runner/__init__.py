"""Autonomous trading runner.

The daemon schedules signal generation, signal conversion and protective exits. It
cannot approve an order itself: every intent goes through ``TradingPipeline`` and
therefore through ``RiskGate``.
"""

from __future__ import annotations

from trading_runner.candles import LatestCandleSource, SqlLatestCandleSource
from trading_runner.runner import (
    RunnerConfigError,
    RunnerCycle,
    RunnerReport,
    TradingRunner,
    ensure_safe_execution_mode,
)

__all__ = [
    "LatestCandleSource",
    "RunnerConfigError",
    "RunnerCycle",
    "RunnerReport",
    "SqlLatestCandleSource",
    "TradingRunner",
    "ensure_safe_execution_mode",
]
