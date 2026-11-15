"""AegisTrade risk engine: the mandatory gate between strategy and execution.

Public API:

    evaluate(intent, portfolio_state, risk_limits, market_context, **flags)
        Pure function implementing the eight risk rules.

    RiskGate
        Limits + kill switch + staleness window bound together, so a service cannot
        accidentally evaluate without the operational flags.

    RejectReason / RiskCheck
        Stable codes used for metrics labels, audit rows and journal tags.
"""

from __future__ import annotations

from aegis_risk.engine import DEFAULT_STALE_AFTER_SECONDS, RiskGate, evaluate
from aegis_risk.reasons import RejectReason, RiskCheck

__all__ = [
    "DEFAULT_STALE_AFTER_SECONDS",
    "RejectReason",
    "RiskCheck",
    "RiskGate",
    "evaluate",
]
