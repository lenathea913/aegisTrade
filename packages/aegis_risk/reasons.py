"""Stable reason codes for risk decisions.

Reason codes are machine-readable on purpose: they become Prometheus labels
(``aegis_risk_rejections_total{reason}``), audit payloads and journal tags. Any
detail goes after ``:`` so the label stays low-cardinality, e.g.
``max_total_exposure_exceeded:12000>10000``.

Renaming a code is a breaking change for dashboards and audit queries.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["RejectReason", "RiskCheck"]


class RejectReason(StrEnum):
    """Why an intent was rejected. One intent may produce several."""

    KILL_SWITCH_ACTIVE = "kill_switch_active"
    SYMBOL_NOT_ALLOWED = "symbol_not_allowed"
    MISSING_MARKET_DATA = "missing_market_data"
    STALE_MARKET_DATA = "stale_market_data"
    MISSING_ENTRY_PRICE = "missing_entry_price"
    INVALID_STOP_FOR_SIDE = "invalid_stop_for_side"
    ZERO_STOP_DISTANCE = "zero_stop_distance"
    RISK_PER_TRADE_EXCEEDED = "risk_per_trade_exceeded"
    DAILY_LOSS_LIMIT_REACHED = "daily_loss_limit_reached"
    WEEKLY_LOSS_LIMIT_REACHED = "weekly_loss_limit_reached"
    MAX_TOTAL_EXPOSURE_EXCEEDED = "max_total_exposure_exceeded"
    MAX_SYMBOL_EXPOSURE_EXCEEDED = "max_exposure_per_symbol_exceeded"
    #: The book already holds ``max_open_positions`` ideas; a new one has no slot.
    MAX_OPEN_POSITIONS_REACHED = "max_open_positions_reached"
    MAX_LEVERAGE_EXCEEDED = "max_leverage_exceeded"
    #: The intent asks for more margin than the cap allows (futures).
    LEVERAGE_CAP_EXCEEDED = "leverage_cap_exceeded"
    #: The notional does not fit in the margin the declared leverage provides.
    INSUFFICIENT_LEVERAGE = "insufficient_leverage"
    #: The liquidation price would be reached before the stop: the stop is fiction.
    LIQUIDATION_TOO_CLOSE = "liquidation_too_close"


class RiskCheck(StrEnum):
    """Checks that ran and passed; recorded on an approval for later audit."""

    KILL_SWITCH_INACTIVE = "kill_switch_inactive"
    SYMBOL_ALLOWED = "symbol_allowed"
    MARKET_DATA_FRESH = "market_data_fresh"
    OPEN_POSITIONS_WITHIN_LIMIT = "open_positions_within_limit"
    DAILY_LOSS_WITHIN_LIMIT = "daily_loss_within_limit"
    WEEKLY_LOSS_WITHIN_LIMIT = "weekly_loss_within_limit"
    #: Futures: margin checks run before the per-trade risk cap.
    LEVERAGE_WITHIN_CAP = "leverage_within_cap"
    MARGIN_SUFFICIENT = "margin_sufficient"
    LIQUIDATION_BEYOND_STOP = "liquidation_beyond_stop"
    RISK_PER_TRADE_WITHIN_LIMIT = "risk_per_trade_within_limit"
    TOTAL_EXPOSURE_WITHIN_LIMIT = "total_exposure_within_limit"
    SYMBOL_EXPOSURE_WITHIN_LIMIT = "symbol_exposure_within_limit"
    LEVERAGE_WITHIN_LIMIT = "leverage_within_limit"
    #: Recorded instead of the new-risk caps when an intent only reduces a position.
    RISK_REDUCING_ORDER = "risk_reducing_order"
