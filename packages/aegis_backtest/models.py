"""Backtest output types: round-trip trades, equity curve and the run summary."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from aegis_backtest.config import BacktestConfig
from aegis_core import Side, StrictModel, Timeframe
from aegis_core.types import (
    FiniteDecimal,
    NonEmptyStr,
    NonNegativeDecimal,
    PositiveDecimal,
    SymbolStr,
    UtcDatetime,
)
from aegis_metrics import PerformanceMetrics

__all__ = ["BacktestResult", "BacktestTrade", "EquityPoint", "ExitReason"]


class ExitReason(StrEnum):
    """Why a round trip ended."""

    SIGNAL = "signal"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    END_OF_DATA = "end_of_data"


class BacktestTrade(StrictModel):
    """One completed round trip (entry fill -> exit fill).

    ``fee`` is the sum of both fills. ``funding`` is the signed cost accrued while
    the position was open (positive = paid). ``net_pnl`` is what the equity curve
    actually moved by, including fees and funding.
    """

    symbol: SymbolStr
    side: Side
    qty: PositiveDecimal
    entry_time: UtcDatetime
    entry_price: PositiveDecimal
    exit_time: UtcDatetime
    exit_price: PositiveDecimal
    fee: NonNegativeDecimal
    funding: FiniteDecimal
    gross_pnl: FiniteDecimal
    net_pnl: FiniteDecimal
    r_multiple: FiniteDecimal | None = None
    exit_reason: ExitReason
    strategy_id: NonEmptyStr


class EquityPoint(StrictModel):
    """Mark-to-market snapshot after one bar closed."""

    timestamp: UtcDatetime
    equity: FiniteDecimal
    cash: FiniteDecimal
    position_qty: FiniteDecimal


class BacktestResult(StrictModel):
    """Everything a run produced, including the assumptions it used."""

    strategy_id: NonEmptyStr
    symbol: SymbolStr
    timeframe: Timeframe
    config: BacktestConfig
    trades: tuple[BacktestTrade, ...] = ()
    equity_curve: tuple[EquityPoint, ...] = ()
    metrics: PerformanceMetrics
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def final_equity(self) -> Decimal:
        return self.equity_curve[-1].equity if self.equity_curve else self.config.initial_equity

    @property
    def total_fees(self) -> Decimal:
        return sum((trade.fee for trade in self.trades), Decimal("0"))

    @property
    def total_funding(self) -> Decimal:
        return sum((trade.funding for trade in self.trades), Decimal("0"))
