"""Prometheus metrics: counters, gauges and the exposition text."""

from __future__ import annotations

from decimal import Decimal

from aegis_observability import Metrics


def test_risk_decisions_are_counted_by_outcome() -> None:
    metrics = Metrics()

    metrics.record_decision(True)
    metrics.record_decision(True)
    metrics.record_decision(False, ("max_leverage_exceeded:2.0>1", "stale_market_data:BTC/USDT"))

    assert metrics.value("aegis_risk_decisions_total", decision="approved") == 2
    assert metrics.value("aegis_risk_decisions_total", decision="rejected") == 1
    # Labels stay low-cardinality: the detail after ":" is dropped.
    assert metrics.value("aegis_risk_rejections_total", reason="max_leverage_exceeded") == 1
    assert metrics.value("aegis_risk_rejections_total", reason="stale_market_data") == 1


def test_rejection_without_reasons_uses_unknown() -> None:
    metrics = Metrics()

    metrics.record_decision(False)

    assert metrics.value("aegis_risk_rejections_total", reason="unknown") == 1


def test_orders_trades_and_requests_are_counted() -> None:
    metrics = Metrics()

    metrics.record_order("filled")
    metrics.record_order("not_filled")
    metrics.record_trade("BTC/USDT", "BUY")
    metrics.record_request("GET", "/health", 200)

    assert metrics.value("aegis_orders_total", outcome="filled") == 1
    assert metrics.value("aegis_orders_total", outcome="not_filled") == 1
    assert metrics.value("aegis_trades_total", symbol="BTC/USDT", side="BUY") == 1
    assert (
        metrics.value("aegis_api_requests_total", method="GET", path="/health", status="200") == 1
    )


def test_gauges_hold_current_values() -> None:
    metrics = Metrics()

    metrics.set_equity(Decimal("9999.7"))
    metrics.set_collection_lag(42.5)

    assert metrics.value("aegis_ledger_equity") == 9999.7
    assert metrics.value("aegis_collector_lag_seconds") == 42.5


def test_safety_gauges_hold_current_values() -> None:
    metrics = Metrics()

    metrics.set_naked_positions(2)
    metrics.set_entries_halted(True)

    assert metrics.value("aegis_positions_naked") == 2
    assert metrics.value("aegis_entries_halted") == 1

    metrics.set_entries_halted(False)

    assert metrics.value("aegis_entries_halted") == 0


def test_instances_do_not_share_registries() -> None:
    first = Metrics()
    second = Metrics()

    first.record_order("filled")

    assert second.value("aegis_orders_total", outcome="filled") == 0


def test_render_produces_exposition_text() -> None:
    metrics = Metrics()
    metrics.record_decision(True)

    body = metrics.render().decode()

    assert "# TYPE aegis_risk_decisions_total counter" in body
    assert 'aegis_risk_decisions_total{decision="approved"} 1.0' in body
