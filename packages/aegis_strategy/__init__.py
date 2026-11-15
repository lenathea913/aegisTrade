"""Strategies: proposals only. A strategy never talks to an exchange."""

from __future__ import annotations

from aegis_strategy.base import StrategyBase, ema
from aegis_strategy.ema_cross import EmaCrossParams, EmaCrossStrategy

__all__ = ["EmaCrossParams", "EmaCrossStrategy", "StrategyBase", "ema"]
