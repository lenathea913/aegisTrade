"""Strategies: proposals only. A strategy never talks to an exchange."""

from __future__ import annotations

from aegis_strategy.base import StrategyBase, ema
from aegis_strategy.bollinger import BollingerParams, BollingerReversionStrategy
from aegis_strategy.ema_cross import EmaCrossParams, EmaCrossStrategy
from aegis_strategy.indicators import AdxTracker, BollingerBands, adx, bollinger_bands

__all__ = [
    "AdxTracker",
    "BollingerBands",
    "BollingerParams",
    "BollingerReversionStrategy",
    "EmaCrossParams",
    "EmaCrossStrategy",
    "StrategyBase",
    "adx",
    "bollinger_bands",
    "ema",
]
