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

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from aegis_alerting import Alerter
from aegis_config import ExecutionMode, Settings
from aegis_core import (
    MarketContext,
    Side,
    Signal,
    SignalStatus,
    Timeframe,
    get_random_motivation,
    utc_now,
)
from aegis_db import ActivityWriter, NullActivityWriter
from aegis_execution import ExecutionGateway, TradingPipeline, build_broker
from aegis_ledger import LedgerWriter, PortfolioLedger
from aegis_observability import get_logger
from aegis_risk import RiskGate
from position_monitor import BrokerVenueCheck, MonitorAction, PositionMonitor
from position_monitor.quotes import QuoteSource
from signal_service import SignalRiskRejectedError, SignalService, SignalStateError
from trading_runner.candles import LatestCandleSource
from trading_runner.ranking import rank_candidates

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
    """One ``--once`` pass: the sync check, the entry scan and the monitor's exits."""

    entries: RunnerCycle
    exits: tuple[MonitorAction, ...] = ()
    #: Number of ledger-vs-venue disagreements found before the entry scan.
    drift: int = 0


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
        alerter: Alerter | None = None,
        clock: Callable[[], datetime] = utc_now,
        entries_blocked: Callable[[], str | None] | None = None,
        position_reconciliation: Callable[[], object] | None = None,
        activity: ActivityWriter | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._signals = signals
        self._candles = candles
        self._quotes = quotes
        self._writer_factory = writer_factory
        self._live_quotes = live_quotes
        self._alerter = alerter
        self._clock = clock
        #: Returns why entries must not run (drift/unavailable), or ``None`` when clear.
        self._entries_blocked = entries_blocked
        self._position_reconciliation = position_reconciliation
        #: Operator-facing feed: the same facts as the log, in rows the dashboard polls.
        self._activity = activity or NullActivityWriter()
        self._timeframe: Timeframe = settings.market_data.timeframes[0]
        self._logger = get_logger("aegis.runner")

    # ---------------------------------------------------------------- entries

    def run_entry_cycle(self) -> RunnerCycle:
        """Generate signals, trade the ``RISK_OK`` ones, and report what happened."""
        if self._settings.risk.kill_switch:
            self._logger.warning(
                "runner_kill_switch", reason="risk.kill_switch is true; no entries this cycle"
            )
            self._activity.record(
                "ORDER_REJECTED",
                "Kill switch is active: entries are skipped, exits keep running.",
                level="WARNING",
            )
            return RunnerCycle(skipped="kill_switch")

        blocked = self._entries_blocked() if self._entries_blocked is not None else None
        if blocked is not None:
            # A book that disagrees with the venue cannot be sized safely. Exits keep
            # running (separate job) so open positions still get their stop.
            self._logger.critical(
                "runner_entries_halted",
                reason=blocked,
                detail="reconcile the book, then `python -m order_reconciler --acknowledge-drift`",
            )
            self._activity.record(
                "RUNNER_HEARTBEAT",
                f"Entries halted: {blocked}. Run `python -m order_reconciler --acknowledge-drift` "
                "once the book matches the venue.",
                level="CRITICAL",
            )
            return RunnerCycle(skipped=blocked)

        now = self._clock()
        fresh = self._fresh_symbols(now)
        if not fresh:
            self._logger.warning(
                "runner_no_fresh_data",
                reason="no symbol has a candle inside the configured freshness window",
            )
            self._activity.record(
                "RUNNER_HEARTBEAT",
                "Waiting for a closed candle: no symbol is inside the freshness window.",
                level="WARNING",
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
        symbols = ", ".join(sorted(fresh))
        if generated:
            self._activity.record(
                "SIGNAL_GENERATED",
                f"Evaluated {symbols}: {len(generated)} signal(s), "
                f"{len(candidates)} actionable after freshness and position checks.",
            )
        else:
            self._activity.record(
                "RUNNER_HEARTBEAT",
                f"Evaluated {symbols}: no crossover signal.",
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
        """Signals the runner may act on: ``RISK_OK``, unexpired, on fresh data.

        Candidates are ranked by signal strength (ADX and distance beyond the slow
        EMA), because ``max_open_positions`` means only the top few can be entered in
        one scan; the risk gate refuses the rest and says so. Reward:risk cannot rank
        them: the fixed 2 % stop / 4 % target makes it 2.0 for every signal.

        A symbol that already holds a position in the signal's direction is skipped:
        the demo strategy re-signals the same cross on the next cycle, and acting on it
        again would pyramid one idea into a larger position. A position only grows
        after it has been closed (the stop/target monitor does that), never by stacking
        entries. An opposite-side signal is still allowed, because it reduces exposure.
        """
        candidates: list[Signal] = []
        for signal in self._signals.list_signals():
            if signal.status is not SignalStatus.RISK_OK:
                continue
            if signal.is_expired(now) or signal.symbol not in fresh:
                continue
            if self._holds_direction(signal.symbol, signal.direction.side):
                self._logger.info(
                    "runner_position_open",
                    symbol=signal.symbol,
                    signal_id=signal.signal_id,
                    side=signal.direction.side.value,
                    reason="position already open in this direction; no pyramiding",
                )
                continue
            candidates.append(signal)
        # The book can hold only ``max_open_positions`` ideas at once, so the order the
        # candidates are offered in decides which ones get a slot: the strongest trend
        # is offered first, not whichever ticker sorts first.
        return rank_candidates(candidates)

    def _holds_direction(self, symbol: str, side: Side) -> bool:
        """True when ``symbol`` already has a non-flat position on ``side``."""
        position = self._ledger.position(symbol)
        if position.is_flat:
            return False
        held = Side.BUY if position.qty > 0 else Side.SELL
        return held is side

    def _trade(self, signal: Signal, now: datetime) -> Any | None:
        """Convert one signal and hand its draft intent to the pipeline."""
        quote = self._quotes.latest(signal.symbol)
        if quote is None:
            self._logger.warning("runner_no_quote", symbol=signal.symbol)
            return None

        self._activity.record(
            "SIGNAL_GENERATED",
            f"{signal.direction.value} signal on {signal.symbol} — asking the risk engine.",
        )
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
            self._activity.record(
                "ORDER_REJECTED",
                f"Order rejected for {signal.symbol}: {', '.join(exc.reasons)}.",
                level="WARNING",
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
            self._activity.record(
                "ORDER_FILLED",
                f"Filled {result.intent.side.value} {result.intent.qty} {signal.symbol} "
                f"at {result.intent.price}.",
            )
        else:
            self._logger.warning(
                "runner_order_rejected",
                symbol=signal.symbol,
                approved=result.decision.approved,
                reasons=list(result.reasons),
                motivation=get_random_motivation(),
            )
            self._activity.record(
                "ORDER_REJECTED",
                "Order rejected for "
                f"{signal.symbol}: {', '.join(result.reasons) or 'no reason given'}.",
                level="WARNING",
            )
        return result

    def _fresh_symbols(self, now: datetime) -> set[str]:
        """Symbols whose newest candle is inside the configured staleness window."""
        stale_after_seconds = self._settings.market_data.stale_after_for(self._timeframe)
        window = timedelta(seconds=stale_after_seconds)
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
                    stale_after_seconds=stale_after_seconds,
                )
                continue
            fresh.add(symbol)
        return fresh

    # ----------------------------------------------------------------- exits

    def run_monitor_cycle(self) -> tuple[MonitorAction, ...]:
        """Check every protected position once; exits go through the pipeline."""
        if not self._settings.runner.monitor_enabled:
            return ()
        self._accrue_funding()
        with self._writer_factory() as writer, self._cycle_broker() as broker:
            monitor = self._monitor(writer, broker=broker)
            actions = monitor.check()
            withheld = monitor.withheld_venue_executions
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
        if withheld:
            # The venue executed before the monitor did. Booking those fills and
            # re-arming the remainder is one reconciliation pass, run *after* this
            # cycle's write transaction closed: the reconciler opens its own.
            self._logger.warning("monitor_venue_execution_reconcile", symbols=list(withheld))
            self.run_position_reconciliation()
        return actions

    def _accrue_funding(self) -> None:
        """Paper futures only: charge simulated funding every 8 hours.

        A real venue debits funding; without this a paper position held for days would
        look better than the identical position on the exchange. Spot and testnet
        brokers are untouched (the latter reports its own funding).
        """
        execution = self._settings.execution
        if execution.market_type.value != "futures" or execution.broker != "paper":
            return
        rate = execution.paper_funding_bps / Decimal("10000")
        charged = self._ledger.accrue_funding(self._clock(), rate=rate)
        if charged:
            self._logger.info("runner_funding_accrued", amount=str(charged))

    def run_position_reconciliation(self) -> int:
        """Compare the book with the venue once; return how many drifts were found.

        Runs *before* entries in ``run_once``: a drift discovered after the scan would
        already have been traded on.
        """
        if self._position_reconciliation is None:
            return 0
        try:
            drifts = self._position_reconciliation()
        except Exception as exc:  # a failed check must not stop the loop
            self._logger.error("position_reconcile_failed", error=str(exc))
            return 0
        if isinstance(drifts, tuple):
            return len(drifts)
        return drifts if isinstance(drifts, int) else 0

    def run_once(self) -> RunnerReport:
        """One pass of every cycle — used by ``--once`` and by the tests."""
        drift = self.run_position_reconciliation()
        return RunnerReport(
            entries=self.run_entry_cycle(),
            exits=self.run_monitor_cycle(),
            drift=drift,
        )

    # -------------------------------------------------------------- internals

    def _pipeline(self, writer: LedgerWriter | None, *, broker: Any = None) -> TradingPipeline:
        gateway = ExecutionGateway(
            broker
            if broker is not None
            else build_broker(
                self._settings.execution,
                exchange=self._settings.exchange,
                live_confirm=self._settings.live_confirm,
            ),
            allow_live_orders=self._settings.execution.allow_live_orders,
        )
        return TradingPipeline(
            gate=RiskGate.from_settings(self._settings, self._timeframe),
            gateway=gateway,
            ledger=self._ledger,
            writer=writer,
            alerter=self._alerter,
        )

    def _monitor(self, writer: LedgerWriter | None, *, broker: Any = None) -> PositionMonitor:
        venue_check = (
            BrokerVenueCheck(broker)
            if broker is not None and callable(getattr(broker, "fetch_positions", None))
            else None
        )
        return PositionMonitor(
            ledger=self._ledger,
            pipeline=self._pipeline(writer, broker=broker),
            quotes=self._quotes,
            clock=self._clock,
            use_live_price=self._settings.position_monitor.use_live_price,
            live_quotes=self._live_quotes,
            protect_take_profit=self._settings.position_monitor.protect_take_profit,
            # Ask the venue before closing: the stop may have fired first.
            venue_check=venue_check,
            activity=self._activity,
        )

    @contextmanager
    def _cycle_broker(self) -> Iterator[Any | None]:
        """One broker for the whole monitor cycle, shared by the gateway and the check.

        ``None`` means paper: the pipeline then builds its own paper broker and there is
        no venue to ask about a fill.
        """
        if self._settings.execution.broker == "paper":
            yield None
            return
        broker = build_broker(
            self._settings.execution,
            exchange=self._settings.exchange,
            live_confirm=self._settings.live_confirm,
        )
        try:
            yield broker
        finally:
            close = getattr(broker, "close", None)
            if callable(close):
                close()
