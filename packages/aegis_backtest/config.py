"""Backtest assumptions.

Every cost knob defaults to a realistic-but-modest value and is explicit in the
result, because a backtest without its assumptions written down is not reproducible.
Fees/slippage/funding are in basis points (1 bp = 0.01 %).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from aegis_core import RiskLimit, StrictModel
from aegis_core.types import FiniteDecimal, NonNegativeDecimal, PositiveDecimal

__all__ = ["BacktestConfig"]


class BacktestConfig(StrictModel):
    """Costs and execution assumptions for one run."""

    initial_equity: PositiveDecimal = Decimal("10000")

    #: Fee charged per fill on market orders (taker).
    taker_fee_bps: NonNegativeDecimal = Decimal("5")
    #: Fee charged per fill on resting limit orders (maker).
    maker_fee_bps: NonNegativeDecimal = Decimal("2")
    #: Adverse price movement applied to every market fill.
    slippage_bps: NonNegativeDecimal = Decimal("5")

    #: Funding per 8h window, in bps of notional. Positive = longs pay shorts.
    funding_rate_bps_per_8h: FiniteDecimal = Decimal("0")

    #: Extra bars between signal and fill (0 = fill at the next bar's open).
    latency_bars: int = Field(default=0, ge=0, le=10)

    #: Fraction of the requested quantity that fills (0-1]; remainder is canceled.
    #: ``None`` means full fills, the default.
    partial_fill_ratio: Decimal | None = Field(default=None, gt=0, le=1)

    #: Whether a SELL signal may open a short (spot = False).
    allow_short: bool = False

    #: Close any open position on the last bar so trades are not left open.
    close_at_end: bool = True

    #: Send every entry intent through the real ``RiskGate`` before it can be
    #: scheduled. Off by default so earlier runs stay comparable; when on, the run
    #: reports how many intents the gate would have refused.
    risk_gate_enabled: bool = False

    #: Limits handed to the gate. ``None`` derives them for the traded symbol from
    #: ``RiskLimit`` defaults; set this to mirror the deployed configuration.
    risk_limits: RiskLimit | None = None
