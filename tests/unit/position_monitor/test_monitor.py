"""The position monitor closes on its levels — always through the risk gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aegis_core import MarketContext, MarketQuote, OrderIntent, OrderType, RiskLimit, Side
from aegis_execution import ExecutionGateway, PaperBroker, TradingPipeline
from aegis_ledger import PortfolioLedger
from aegis_risk import RiskGate
from position_monitor import ExitTrigger, PositionMonitor

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ENTRY_TIME = NOW - timedelta(hours=3)


class Quotes:
    """A quote source with a fixed answer per symbol."""

    def __init__(self, prices: dict[str, str], *, age_seconds: int = 5) -> None:
        self._prices = prices
        self._age = age_seconds

    def latest(self, symbol: str) -> MarketQuote | None:
        price = self._prices.get(symbol)
        if price is None:
            return None
        return MarketQuote(
            symbol=symbol,
            price=Decimal(price),
            as_of=NOW - timedelta(seconds=self._age),
        )


def _limits() -> RiskLimit:
    return RiskLimit(
        max_risk_per_trade_pct=Decimal("2"),
        max_daily_loss_pct=Decimal("5"),
        max_weekly_loss_pct=Decimal("10"),
        max_exposure_per_symbol_pct=Decimal("50"),
        max_total_exposure_pct=Decimal("100"),
        max_leverage=Decimal("1"),
        allowed_symbols=frozenset({"BTC/USDT"}),
    )


def _pipeline(ledger: PortfolioLedger, *, kill_switch: bool = False) -> TradingPipeline:
    gate = RiskGate(limits=_limits(), kill_switch=kill_switch, stale_after_seconds=60)
    gateway = ExecutionGateway(PaperBroker(fee_bps=Decimal("0"), slippage_bps=Decimal("0")))
    return TradingPipeline(gate=gate, gateway=gateway, ledger=ledger)


def _open_long(pipeline: TradingPipeline, ledger: PortfolioLedger) -> None:
    intent = OrderIntent(
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=Decimal("0.1"),
        price=Decimal("30000"),
        stop_loss=Decimal("29000"),
        take_profit=Decimal("32000"),
        strategy_id="simple_ema_cross",
        reason="crossed above",
        created_at=NOW,
    )
    market = MarketContext(
        quotes=(MarketQuote(symbol="BTC/USDT", price=Decimal("30000"), as_of=NOW),),
        now=NOW,
    )
    result = pipeline.process(intent, market)
    assert result.filled is True
    assert ledger.position("BTC/USDT").qty == Decimal("0.1")


def _monitor(
    pipeline: TradingPipeline, ledger: PortfolioLedger, quotes: Quotes, **kwargs: object
) -> PositionMonitor:
    return PositionMonitor(
        ledger=ledger,
        pipeline=pipeline,
        quotes=quotes,
        clock=lambda: NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def test_stop_loss_closes_the_position() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = _pipeline(ledger)
    _open_long(pipeline, ledger)
    monitor = _monitor(pipeline, ledger, Quotes({"BTC/USDT": "28999"}))

    actions = monitor.check()

    assert len(actions) == 1
    action = actions[0]
    assert action.trigger is ExitTrigger.STOP_LOSS
    assert action.closed is True
    assert ledger.position("BTC/USDT").is_flat is True
    assert ledger.protection("BTC/USDT") is None


def test_take_profit_closes_the_position() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = _pipeline(ledger)
    _open_long(pipeline, ledger)
    monitor = _monitor(pipeline, ledger, Quotes({"BTC/USDT": "32500"}))

    actions = monitor.check()

    assert len(actions) == 1
    assert actions[0].trigger is ExitTrigger.TAKE_PROFIT
    assert actions[0].closed is True
    assert ledger.position("BTC/USDT").is_flat is True


def test_price_inside_the_levels_does_nothing() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = _pipeline(ledger)
    _open_long(pipeline, ledger)
    monitor = _monitor(pipeline, ledger, Quotes({"BTC/USDT": "30500"}))

    assert monitor.check() == ()
    assert ledger.position("BTC/USDT").qty == Decimal("0.1")


def test_protection_can_be_disabled_for_targets() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = _pipeline(ledger)
    _open_long(pipeline, ledger)
    monitor = _monitor(pipeline, ledger, Quotes({"BTC/USDT": "32500"}), protect_take_profit=False)

    assert monitor.check() == ()


def test_flat_book_produces_no_actions() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = _pipeline(ledger)
    monitor = _monitor(pipeline, ledger, Quotes({"BTC/USDT": "29000"}))

    assert monitor.check() == ()


def test_missing_quote_is_reported_and_skipped() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = _pipeline(ledger)
    _open_long(pipeline, ledger)
    monitor = _monitor(pipeline, ledger, Quotes({}))

    assert monitor.check() == ()
    assert ledger.position("BTC/USDT").qty == Decimal("0.1")


def test_kill_switch_still_blocks_the_protective_exit() -> None:
    """The monitor does not get a private door around the gate."""
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    _open_long(_pipeline(ledger), ledger)
    pipeline = _pipeline(ledger, kill_switch=True)
    monitor = _monitor(pipeline, ledger, Quotes({"BTC/USDT": "28999"}))

    actions = monitor.check()

    assert len(actions) == 1
    assert actions[0].result.decision.approved is False
    assert "kill_switch_active" in actions[0].result.reasons
    assert ledger.position("BTC/USDT").qty == Decimal("0.1")


def test_stale_quote_blocks_the_exit_and_keeps_the_position() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = _pipeline(ledger)
    _open_long(pipeline, ledger)
    monitor = _monitor(pipeline, ledger, Quotes({"BTC/USDT": "28999"}, age_seconds=600))

    actions = monitor.check()

    assert len(actions) == 1
    assert actions[0].result.decision.approved is False
    assert any(reason.startswith("stale_market_data") for reason in actions[0].result.reasons)
    assert ledger.position("BTC/USDT").qty == Decimal("0.1")


@pytest.mark.parametrize("price", ["28999", "32500"])
def test_exit_intent_carries_the_full_position(price: str) -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = _pipeline(ledger)
    _open_long(pipeline, ledger)
    monitor = _monitor(pipeline, ledger, Quotes({"BTC/USDT": price}))

    action = monitor.check()[0]

    assert action.intent.side is Side.SELL
    assert action.intent.qty == Decimal("0.1")
    assert action.intent.strategy_id == "position_monitor"
    assert action.intent.stop_loss > action.intent.price  # type: ignore[operator]


def test_live_price_uses_the_ledger_mark_when_enabled() -> None:
    """``position_monitor.use_live_price`` acts on a pushed mark, not the last candle."""
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = _pipeline(ledger)
    _open_long(pipeline, ledger)
    ledger.mark_price("BTC/USDT", Decimal("28999"))
    monitor = _monitor(pipeline, ledger, Quotes({"BTC/USDT": "30500"}), use_live_price=True)

    actions = monitor.check()

    assert len(actions) == 1
    assert actions[0].trigger is ExitTrigger.STOP_LOSS
    assert actions[0].closed is True


def test_live_price_is_ignored_without_a_mark() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    pipeline = _pipeline(ledger)
    _open_long(pipeline, ledger)
    monitor = _monitor(pipeline, ledger, Quotes({"BTC/USDT": "30500"}), use_live_price=True)

    assert monitor.check() == ()
