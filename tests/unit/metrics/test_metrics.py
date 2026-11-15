"""Metric formulas, including the undefined cases that must return ``None``."""

from __future__ import annotations

import statistics
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_metrics import (
    PerformanceMetrics,
    average_r,
    compute_metrics,
    expectancy,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    win_rate,
)


def test_win_rate_counts_strict_winners() -> None:
    assert win_rate([100.0, -50.0, 10.0, 0.0]) == 0.5
    assert win_rate([]) == 0.0
    assert win_rate([-1.0, -2.0]) == 0.0


def test_expectancy_is_mean_pnl() -> None:
    assert expectancy([100.0, -50.0, 10.0]) == pytest.approx(20.0)
    assert expectancy([]) == 0.0


def test_profit_factor_is_gross_profit_over_gross_loss() -> None:
    assert profit_factor([100.0, -50.0, -25.0]) == pytest.approx(100 / 75)
    assert profit_factor([10.0, 20.0]) is None  # no losses -> undefined, not infinity
    assert profit_factor([-10.0, -20.0]) == 0.0


def test_max_drawdown_measures_peak_to_trough() -> None:
    assert max_drawdown([100.0, 120.0, 90.0]) == pytest.approx(0.25)
    assert max_drawdown([100.0, 110.0, 121.0]) == 0.0
    assert max_drawdown([]) == 0.0
    assert max_drawdown([100.0, 50.0, 40.0]) == pytest.approx(0.6)


def test_sharpe_is_none_without_dispersion_or_history() -> None:
    assert sharpe_ratio([100.0], 8760) is None
    assert sharpe_ratio([100.0, 110.0], 8760) is None
    assert sharpe_ratio([100.0, 110.0, 121.0], 8760) is None  # constant returns


def test_sharpe_matches_manual_formula() -> None:
    equity = [100.0, 110.0, 99.0, 108.9, 120.0]
    returns = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    expected = statistics.fmean(returns) / statistics.stdev(returns) * (8760**0.5)

    assert sharpe_ratio(equity, 8760) == pytest.approx(expected)


def test_sortino_is_none_when_nothing_goes_down() -> None:
    assert sortino_ratio([100.0, 105.0, 110.0], 8760) is None
    assert sortino_ratio([], 8760) is None


def test_sortino_matches_manual_formula() -> None:
    equity = [100.0, 110.0, 99.0, 108.9]
    returns = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    downside = [min(value, 0.0) for value in returns]
    deviation = (statistics.fmean(value * value for value in downside)) ** 0.5
    expected = statistics.fmean(returns) / deviation * (8760**0.5)

    assert sortino_ratio(equity, 8760) == pytest.approx(expected)


def test_non_positive_equity_points_are_skipped_in_returns() -> None:
    # A zero equity point cannot produce a percentage return, so that pair is
    # skipped: here only 100 -> 0 (-100 %) and 100 -> 110 (+10 %) remain.
    equity = [100.0, 0.0, 100.0, 110.0]
    expected = statistics.fmean([-1.0, 0.1]) / statistics.stdev([-1.0, 0.1]) * (8760**0.5)

    assert sharpe_ratio(equity, 8760) == pytest.approx(expected)
    assert isinstance(sortino_ratio(equity, 8760), float)


def test_average_r_ignores_absent_r_multiples() -> None:
    assert average_r([2.0, -1.0]) == pytest.approx(0.5)
    assert average_r([]) is None


def test_compute_metrics_assembles_everything() -> None:
    metrics = compute_metrics(
        trade_pnls=[Decimal("100"), Decimal("-40")],
        equity_curve=[Decimal("10000"), Decimal("10050"), Decimal("10100")],
        r_multiples=[2.0, -1.0],
        periods_per_year=8760,
    )

    assert metrics.total_trades == 2
    assert metrics.winning_trades == 1
    assert metrics.losing_trades == 1
    assert metrics.win_rate == 0.5
    assert metrics.expectancy == pytest.approx(30.0)
    assert metrics.profit_factor == pytest.approx(2.5)
    assert metrics.net_pnl == pytest.approx(60.0)
    assert metrics.gross_profit == pytest.approx(100.0)
    assert metrics.gross_loss == pytest.approx(40.0)
    assert metrics.avg_r == pytest.approx(0.5)
    assert metrics.max_drawdown == 0.0
    expected_sharpe = (
        statistics.fmean([0.005, 0.004975124378109453])
        / statistics.stdev([0.005, 0.004975124378109453])
        * (8760**0.5)
    )
    assert metrics.sharpe == pytest.approx(expected_sharpe)


def test_compute_metrics_on_an_empty_run() -> None:
    metrics = compute_metrics([], [Decimal("10000")], [], 8760)

    assert metrics.total_trades == 0
    assert metrics.win_rate == 0.0
    assert metrics.expectancy == 0.0
    assert metrics.profit_factor is None
    assert metrics.max_drawdown == 0.0
    assert metrics.sharpe is None


def test_metrics_model_is_strict_and_frozen() -> None:
    with pytest.raises(ValidationError):
        PerformanceMetrics(
            total_trades=1,
            winning_trades=1,
            losing_trades=0,
            win_rate=1.5,  # rate must be a fraction
            expectancy=1.0,
            max_drawdown=0.0,
        )
