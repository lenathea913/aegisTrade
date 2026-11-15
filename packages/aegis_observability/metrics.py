"""Prometheus metrics for the money path.

Every instance owns its own ``CollectorRegistry``, so tests can build as many apps
as they like without duplicate-registration errors. Labels are deliberately
low-cardinality: rejection reasons are cut at ``:`` so
``max_total_exposure_exceeded:12000>10000`` becomes ``max_total_exposure_exceeded``.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

__all__ = ["CONTENT_TYPE_LATEST", "Metrics"]

_ZERO = Decimal("0")


def _reason_label(reason: str) -> str:
    return reason.split(":", 1)[0]


class Metrics:
    """Counters and gauges exposed at ``/metrics``."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self._decisions = Counter(
            "aegis_risk_decisions_total",
            "Risk decisions by outcome.",
            ["decision"],
            registry=self.registry,
        )
        self._rejections = Counter(
            "aegis_risk_rejections_total",
            "Risk rejections by reason code.",
            ["reason"],
            registry=self.registry,
        )
        self._orders = Counter(
            "aegis_orders_total",
            "Orders that reached the broker, by outcome.",
            ["outcome"],
            registry=self.registry,
        )
        self._trades = Counter(
            "aegis_trades_total",
            "Executed trades by symbol and side.",
            ["symbol", "side"],
            registry=self.registry,
        )
        self._lag = Gauge(
            "aegis_collector_lag_seconds",
            "Age of the newest collected candle, in seconds.",
            registry=self.registry,
        )
        self._equity = Gauge(
            "aegis_ledger_equity",
            "Portfolio equity reported by the ledger, in quote currency.",
            registry=self.registry,
        )
        self._requests = Counter(
            "aegis_api_requests_total",
            "HTTP requests by method, route and status.",
            ["method", "path", "status"],
            registry=self.registry,
        )

    # --------------------------------------------------------------- writes

    def record_decision(self, approved: bool, reasons: Sequence[str] = ()) -> None:
        decision = "approved" if approved else "rejected"
        self._decisions.labels(decision=decision).inc()
        if not approved:
            for reason in reasons or ("unknown",):
                self._rejections.labels(reason=_reason_label(reason)).inc()

    def record_order(self, outcome: str) -> None:
        self._orders.labels(outcome=outcome).inc()

    def record_trade(self, symbol: str, side: str) -> None:
        self._trades.labels(symbol=symbol, side=side).inc()

    def set_collection_lag(self, seconds: float) -> None:
        self._lag.set(seconds)

    def set_equity(self, equity: Decimal | float) -> None:
        self._equity.set(float(equity))

    def record_request(self, method: str, path: str, status: int) -> None:
        self._requests.labels(method=method, path=path, status=str(status)).inc()

    # ----------------------------------------------------------------- reads

    def render(self) -> bytes:
        """Exposition text for ``GET /metrics``."""
        return generate_latest(self.registry)

    def value(self, name: str, **labels: str) -> float:
        """Read one sample back (used by tests and readiness checks)."""
        return self.registry.get_sample_value(name, labels) or 0.0
