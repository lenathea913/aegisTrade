"""Autonomous trading runner.

``TradingRunner`` turns the platform from "click to trade" into "trade on a
schedule" without weakening a single guard:

* it never builds an ``ApprovedOrder``. A ``RISK_OK`` signal is converted into a
  **draft** ``OrderIntent`` (``SignalService`` re-runs the gate while converting) and
  handed to ``TradingPipeline``, which runs ``RiskGate`` again and is the only object
  that can reach a broker;
* it refuses to start in live mode unless ``execution.allow_live_orders`` is true;
* the kill switch is checked before any entry work — and the gate sees it too, so a
  race cannot slip an order through;
* a missing or stale candle means no entry: doing nothing beats guessing;
* each tick writes its order, trade, position and audit rows in one transaction, so
  a protective exit lands in ``trading.trades`` and ``audit.audit_logs`` together.

Two independent cycles run on one scheduler: *entries* every
``runner.signal_interval_seconds`` (default 5 min) and the *position monitor* every
``runner.monitor_interval_seconds`` (default 1 min). They are separate jobs so a slow
signal scan cannot delay a stop-loss check.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aegis_config import ExecutionMode, Settings
from aegis_core import (
    MarketContext,
    Signal,
    SignalStatus,
    Timeframe,
    get_random_motivation,
    utc_now,
)
from aegis_execution import ExecutionGateway, TradingPipeline, build_broker
from aegis_ledger import LedgerWriter, PortfolioLedger
from aegis_observability import get_logger
from aegis_risk import RiskGate
from position_monitor import MonitorAction, PositionMonitor
from position_monitor.quotes import QuoteSource
from signal_service import SignalRiskRejectedError, SignalService, SignalStateError
from trading_runner.candles import LatestCandleSource

__all__ = [
    "RunnerConfigError",
    "RunnerCycle",
    "RunnerReport",
    "TradingRunner",
    "ensure_safe_execution_mode",
]

#: A writer factory returns a context manager around the writer for one tick, so the
#: whole tick commits or rolls back as a unit. ``None`` means "do not persist"
#: (tests and dry runs).
WriterFactory = Callable[[], AbstractContextManager[LedgerWriter | None]]


class RunnerConfigError(RuntimeError):
    """The configuration would let the runner trade where it must not."""


def ensure_safe_execution_mode(settings: Settings) -> None:
    """Refuse to start live trading without the second, independent switch.

    ``ExecutionSettings`` already requires exchange credentials for ``live``; this is
    the runner's own check, so the daemon cannot start in live mode by accident while
    ``allow_live_orders`` is false.
    """
    if settings.execution.mode is ExecutionMode.LIVE and not settings.execution.allow_live_orders:
        raise RunnerConfigError(
            "refusing to start: execution.mode is live but execution.allow_live_orders is false "
            "(set AEGIS_EXECUTION__ALLOW_LIVE_ORDERS=true only after paper mode is proven)"
        )


@dataclass(frozen=True)
class RunnerCycle:
    """What one entry scan did, and why it did nothing when it did nothing."""

    skipped: str | None = None
    generated: int = 0
    converted: tuple[str, ...] = ()
    filled: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()

    @property
    def traded(self) -> bool:
        return bool(self.filled)


@dataclass(frozen=True)
class RunnerReport:
    """One ``--once`` pass: the entry scan plus the monitor's actions."""

    entries: RunnerCycle
    exits: tuple[MonitorAction, ...] = ()


class TradingRunner:
    """Scheduled entries plus scheduled protective exits, both through the pipeline."""

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: PortfolioLedger,
        signals: SignalService,
        candles: LatestCandleSource,
        quotes: QuoteSource,
        writer_factory: WriterFactory,
        live_quotes: QuoteSource | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._signals = signals
        self._candles = candles
        self._quotes = quotes
        self._writer_factory = writer_factory
        self._live_quotes = live_quotes
        self._clock = clock
        self._timeframe: Timeframe = settings.market_data.timeframes[0]
        self._logger = get_logger("aegis.runner")

    # ---------------------------------------------------------------- entries

    def run_entry_cycle(self) -> RunnerCycle:
        """Generate signals, trade the ``RISK_OK`` ones, and report what happened."""
        if self._settings.risk.kill_switch:
            self._logger.warning(
                "runner_kill_switch", reason="risk.kill_switch is true; no entries this cycle"
            )
            return RunnerCycle(skipped="kill_switch")

        now = self._clock()
        fresh = self._fresh_symbols(now)
        if not fresh:
            self._logger.warning(
                "runner_no_fresh_data",
                reason="no symbol has a candle inside market_data.stale_after_seconds",
            )
            return RunnerCycle(skipped="stale_market_data")

        generated = self._signals.generate()
        candidates = self._candidates(fresh, now)
        self._logger.info(
            "runner_cycle",
            generated=len(generated),
            candidates=len(candidates),
            symbols=sorted(fresh),
        )

        converted: list[str] = []
        filled: list[str] = []
        rejected: list[str] = []
        for signal in candidates:
            outcome = self._trade(signal, now)
            if outcome is None:
                continue
            converted.append(signal.signal_id)
            if outcome.filled:
                filled.append(signal.symbol)
            else:
                rejected.append(signal.symbol)
        return RunnerCycle(
            generated=len(generated),
            converted=tuple(converted),
            filled=tuple(filled),
            rejected=tuple(rejected),
        )

    def _candidates(self, fresh: set[str], now: datetime) -> tuple[Signal, ...]:
        """Signals the runner may act on: ``RISK_OK``, unexpired, on fresh data."""
        return tuple(
            signal
            for signal in self._signals.list_signals()
            if signal.status is SignalStatus.RISK_OK
            and not signal.is_expired(now)
            and signal.symbol in fresh
        )

    def _trade(self, signal: Signal, now: datetime) -> Any | None:
        """Convert one signal and hand its draft intent to the pipeline."""
        quote = self._quotes.latest(signal.symbol)
        if quote is None:
            self._logger.warning("runner_no_quote", symbol=signal.symbol)
            return None

        try:
            with self._writer_factory() as writer:
                pipeline = self._pipeline(writer)
                conversion = self._signals.convert(signal.signal_id)
                market = MarketContext(quotes=(quote,), now=self._clock())
                result = pipeline.process(conversion.intent, market)
        except SignalRiskRejectedError as exc:
            # The gate changed its mind between generation and execution: fine.
            self._logger.warning(
                "runner_convert_rejected",
                symbol=signal.symbol,
                signal_id=signal.signal_id,
                reasons=list(exc.reasons),
                motivation=get_random_motivation(),
            )
            return None
        except SignalStateError as exc:
            self._logger.warning(
                "runner_signal_skipped", signal_id=signal.signal_id, error=str(exc)
            )
            return None

        self._logger.info(
            "runner_signal_converted",
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            direction=signal.direction.value,
            qty=str(result.intent.qty),
        )
        if result.filled:
            self._logger.info(
                "runner_order_filled",
                symbol=signal.symbol,
                side=result.intent.side.value,
                qty=str(result.intent.qty),
                trade_id=None if result.trade is None else result.trade.trade_id,
            )
        else:
            self._logger.warning(
                "runner_order_rejected",
                symbol=signal.symbol,
                approved=result.decision.approved,
                reasons=list(result.reasons),
                motivation=get_random_motivation(),
            )
        return result

    def _fresh_symbols(self, now: datetime) -> set[str]:
        """Symbols whose newest candle is inside the configured staleness window."""
        window = timedelta(seconds=self._settings.market_data.stale_after_seconds)
        fresh: set[str] = set()
        for symbol in self._settings.market_data.symbols:
            candle = self._candles.latest(symbol, self._timeframe)
            if candle is None:
                self._logger.warning("runner_candle_missing", symbol=symbol)
                continue
            age = now - candle.close_time
            if age > window:
                self._logger.warning(
                    "runner_candle_stale",
                    symbol=symbol,
                    age_seconds=round(age.total_seconds(), 1),
                    stale_after_seconds=self._settings.market_data.stale_after_seconds,
                )
                continue
            fresh.add(symbol)
        return fresh

    # ----------------------------------------------------------------- exits

    def run_monitor_cycle(self) -> tuple[MonitorAction, ...]:
        """Check every protected position once; exits go through the pipeline."""
        if not self._settings.runner.monitor_enabled:
            return ()
        with self._writer_factory() as writer:
            monitor = self._monitor(writer)
            actions = monitor.check()
        for action in actions:
            self._logger.info(
                "runner_monitor_action",
                symbol=action.symbol,
                trigger=action.trigger.value,
                filled=action.closed,
                approved=action.result.decision.approved,
            )
            if not action.result.decision.approved:
                self._logger.warning(
                    "runner_monitor_rejected",
                    symbol=action.symbol,
                    reasons=list(action.result.reasons),
                    motivation=get_random_motivation(),
                )
        return actions

    def run_once(self) -> RunnerReport:
        """One pass of both cycles — used by ``--once`` and by the tests."""
        return RunnerReport(entries=self.run_entry_cycle(), exits=self.run_monitor_cycle())

    # -------------------------------------------------------------- internals

    def _pipeline(self, writer: LedgerWriter | None) -> TradingPipeline:
        gateway = ExecutionGateway(
            build_broker(
                self._settings.execution,
                exchange=self._settings.exchange,
                live_confirm=self._settings.live_confirm,
            ),
            allow_live_orders=self._settings.execution.allow_live_orders,
        )
        return TradingPipeline(
            gate=RiskGate.from_settings(self._settings),
            gateway=gateway,
            ledger=self._ledger,
            writer=writer,
        )

    def _monitor(self, writer: LedgerWriter | None) -> PositionMonitor:
        return PositionMonitor(
            ledger=self._ledger,
            pipeline=self._pipeline(writer),
            quotes=self._quotes,
            clock=self._clock,
            use_live_price=self._settings.position_monitor.use_live_price,
            live_quotes=self._live_quotes,
            protect_take_profit=self._settings.position_monitor.protect_take_profit,
        )
