"""Performance statistics.

These are descriptive statistics of one backtest, not a forecast and not a claim
about future results. Money stays ``Decimal`` in the ledger; metrics are ``float``
because they are ratios and standard deviations, which are inherently approximate.

Conventions
-----------
* ``profit_factor`` is ``gross_profit / |gross_loss|``; it is ``None`` when there are
  no losing trades (an infinite ratio is not a usable number).
* ``sharpe``/``sortino`` use per-period returns with a zero risk-free rate and are
  annualised by ``sqrt(periods_per_year)``. ``None`` when undefined (fewer than two
  observations, or zero dispersion).
* ``max_drawdown`` is a positive fraction of the running peak (``0.25`` = -25 %).
* ``expectancy`` is the mean net PnL per closed trade, in quote currency.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PerformanceMetrics",
    "average_r",
    "compute_metrics",
    "expectancy",
    "max_drawdown",
    "profit_factor",
    "sharpe_ratio",
    "sortino_ratio",
    "win_rate",
]


class PerformanceMetrics(BaseModel):
    """Summary statistics for one backtest run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_trades: int = Field(ge=0)
    winning_trades: int = Field(ge=0)
    losing_trades: int = Field(ge=0)
    win_rate: float = Field(ge=0.0, le=1.0)
    expectancy: float
    profit_factor: float | None = None
    max_drawdown: float = Field(ge=0.0, le=1.0)
    sharpe: float | None = None
    sortino: float | None = None
    avg_r: float | None = None
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0


def win_rate(pnls: Sequence[float]) -> float:
    """Share of strictly positive trades."""
    if not pnls:
        return 0.0
    return sum(1 for pnl in pnls if pnl > 0) / len(pnls)


def expectancy(pnls: Sequence[float]) -> float:
    """Mean net PnL per trade."""
    if not pnls:
        return 0.0
    return statistics.fmean(pnls)


def profit_factor(pnls: Sequence[float]) -> float | None:
    """Gross profit divided by gross loss; ``None`` when there is no gross loss."""
    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))
    if gross_loss == 0:
        return None
    return gross_profit / gross_loss


def max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction."""
    peak = -math.inf
    worst = 0.0
    for value in equity:
        if value > peak:
            peak = value
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def _returns(equity: Sequence[float]) -> list[float]:
    return [
        equity[index] / equity[index - 1] - 1.0
        for index in range(1, len(equity))
        if equity[index - 1] > 0
    ]


def sharpe_ratio(equity: Sequence[float], periods_per_year: float) -> float | None:
    """Annualised Sharpe ratio of per-period returns (risk-free = 0)."""
    returns = _returns(equity)
    if len(returns) < 2:
        return None
    dispersion = statistics.stdev(returns)
    if dispersion == 0:
        return None
    return statistics.fmean(returns) / dispersion * math.sqrt(periods_per_year)


def sortino_ratio(equity: Sequence[float], periods_per_year: float) -> float | None:
    """Annualised Sortino ratio using downside deviation of negative returns."""
    returns = _returns(equity)
    if not returns:
        return None
    downside = [min(value, 0.0) for value in returns]
    downside_deviation = math.sqrt(statistics.fmean(value * value for value in downside))
    if downside_deviation == 0:
        return None
    return statistics.fmean(returns) / downside_deviation * math.sqrt(periods_per_year)


def average_r(r_multiples: Sequence[float]) -> float | None:
    """Mean R multiple across trades that have one."""
    if not r_multiples:
        return None
    return statistics.fmean(r_multiples)


def compute_metrics(
    trade_pnls: Sequence[Decimal],
    equity_curve: Sequence[Decimal],
    r_multiples: Sequence[float],
    periods_per_year: float,
) -> PerformanceMetrics:
    """Assemble the full metric set for a backtest result."""
    pnls = [float(pnl) for pnl in trade_pnls]
    equity = [float(value) for value in equity_curve]
    wins = sum(1 for pnl in pnls if pnl > 0)
    losses = sum(1 for pnl in pnls if pnl < 0)

    return PerformanceMetrics(
        total_trades=len(pnls),
        winning_trades=wins,
        losing_trades=losses,
        win_rate=win_rate(pnls),
        expectancy=expectancy(pnls),
        profit_factor=profit_factor(pnls),
        max_drawdown=max_drawdown(equity),
        sharpe=sharpe_ratio(equity, periods_per_year),
        sortino=sortino_ratio(equity, periods_per_year),
        avg_r=average_r(r_multiples),
        gross_profit=sum(pnl for pnl in pnls if pnl > 0),
        gross_loss=abs(sum(pnl for pnl in pnls if pnl < 0)),
        net_pnl=sum(pnls),
    )
