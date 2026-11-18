"""Signal generation, risk preview and conversion.

Flow for one idea:

    closed candles -> EmaCrossStrategy -> OrderIntent -> Signal (NEW)
                                        -> RiskGate.evaluate (dry run)
                                        -> RISK_OK | BLOCKED_BY_RISK

    "Convert" -> rebuild the intent -> evaluate again -> SignalRiskRejectedError (400)
                                            \\-> OrderIntent **draft** (never executed)

Two things are deliberate:

* the risk preview is a **dry run**: the gate is asked, nothing is ordered and the
  ledger is untouched;
* conversion **re-runs** the gate. A preview is a convenience for the dashboard, not
  an authorisation; limits or market data may have changed since the signal appeared.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from aegis_core import (
    MARGIN_UTILISATION,
    MAX_LEVERAGE,
    Candle,
    MarketContext,
    MarketQuote,
    OrderIntent,
    OrderType,
    PositionSide,
    RiskDecision,
    Side,
    Signal,
    SignalDirection,
    SignalStatus,
    Timeframe,
    choose_leverage,
    utc_now,
)
from aegis_ledger import PortfolioLedger
from aegis_risk import RiskGate
from aegis_strategy import EmaCrossParams, EmaCrossStrategy
from signal_service.errors import (
    SignalNotFoundError,
    SignalPositionOpenError,
    SignalRiskRejectedError,
    SignalStateError,
)
from signal_service.repository import CandleRepository
from signal_service.store import InMemorySignalStore, SignalStore

__all__ = [
    "DEFAULT_HISTORY_BARS",
    "DEFAULT_SIGNAL_TTL_SECONDS",
    "ConversionResult",
    "ScanReport",
    "SignalService",
]

DEFAULT_SIGNAL_TTL_SECONDS = 3600
DEFAULT_HISTORY_BARS = 300
_HUNDRED = Decimal("100")
_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True)
class ScanReport:
    """What one scan found, including *why* it found nothing.

    A scan that produces no signal is the normal case — the latest closed bar simply
    has no cross — but an empty result with no explanation reads as a broken button.
    This report is what the dashboard turns into a sentence.
    """

    scanned: int
    created: tuple[Signal, ...]
    skipped: dict[str, int]
    bars_by_symbol: dict[str, int]
    latest_bar_close: datetime | None
    warmup_bars: int
    fast_period: int
    slow_period: int

    @property
    def no_cross(self) -> bool:
        return self.created == () and self.skipped.get("no_cross", 0) > 0

    @property
    def insufficient_history(self) -> bool:
        return self.created == () and self.skipped.get("insufficient_history", 0) == self.scanned


@dataclass(frozen=True)
class ConversionResult:
    """A signal that survived a second risk check, plus the draft intent."""

    signal: Signal
    intent: OrderIntent
    decision: RiskDecision

    @property
    def draft(self) -> OrderIntent:
        """The intent is a draft: converting a signal never places an order."""
        return self.intent


class SignalService:
    """Turns candles into signals and gates their conversion."""

    def __init__(
        self,
        *,
        gate: RiskGate,
        ledger: PortfolioLedger,
        candles: CandleRepository,
        symbols: Sequence[str],
        timeframe: Timeframe,
        store: SignalStore | None = None,
        clock: Any = utc_now,
        ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS,
        history_bars: int = DEFAULT_HISTORY_BARS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("signal ttl must be positive")
        if not symbols:
            raise ValueError("signal service needs at least one symbol")
        self._gate = gate
        self._ledger = ledger
        self._candles = candles
        self._symbols = tuple(symbols)
        self._timeframe = timeframe
        self._store: SignalStore = store if store is not None else InMemorySignalStore()
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_seconds)
        self._history_bars = history_bars

    # ------------------------------------------------------------- generation

    def generate(self) -> tuple[Signal, ...]:
        """Scan every configured symbol once and store the new signals."""
        return self.scan().created

    def scan(self) -> ScanReport:
        """Scan every symbol once and say what happened, symbol by symbol.

        ``generate`` is the same call without the diagnostics; the dashboard uses this
        one so "nothing happened" can be explained instead of silently rendering the
        previous list.
        """
        now = self._clock()
        created: list[Signal] = []
        skipped: dict[str, int] = {}
        bars: dict[str, int] = {}
        latest: datetime | None = None
        params = EmaCrossParams()
        warmup = params.slow_period + 1

        for symbol in self._symbols:
            history = self._candles.recent(symbol, self._timeframe, self._history_bars)
            bars[symbol] = len(history)
            if history:
                close_time = history[-1].close_time
                latest = close_time if latest is None or close_time > latest else latest
            signal = self._generate_for(symbol, now, history, warmup=warmup)
            if signal is None:
                reason = "insufficient_history" if len(history) < warmup else "no_cross"
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            self._store.add(signal)
            created.append(signal)

        return ScanReport(
            scanned=len(self._symbols),
            created=tuple(created),
            skipped=skipped,
            bars_by_symbol=bars,
            latest_bar_close=latest,
            warmup_bars=warmup,
            fast_period=params.fast_period,
            slow_period=params.slow_period,
        )

    def _generate_for(
        self,
        symbol: str,
        now: datetime,
        history: Sequence[Candle],
        *,
        warmup: int | None = None,
    ) -> Signal | None:
        strategy = EmaCrossStrategy(symbol=symbol, timeframe=self._timeframe)
        if len(history) < (warmup if warmup is not None else strategy.warmup_bars):
            return None

        intent = strategy.on_candle(history[-1], history)
        if intent is None:
            return None

        signal = self._signal_from(intent, history, now)
        decision = self._preview(signal, now)
        if decision.approved:
            return signal.with_status(SignalStatus.RISK_OK)
        return signal.with_status(SignalStatus.BLOCKED_BY_RISK, decision.reasons)

    def _signal_from(self, intent: OrderIntent, history: Sequence[Candle], now: datetime) -> Signal:
        entry = intent.price if intent.price is not None else history[-1].close
        target = intent.take_profit
        if target is None:
            raise SignalStateError("the strategy produced an intent without a target")
        direction = SignalDirection.LONG if intent.side.sign > 0 else SignalDirection.SHORT
        last = history[-1]
        return Signal(
            symbol=intent.symbol,
            timeframe=self._timeframe,
            strategy_id=intent.strategy_id,
            strategy_version="1",
            direction=direction,
            entry_ref=entry,
            stop_loss=intent.stop_loss,
            take_profit=target,
            risk_reward=Signal.risk_reward_for(entry, intent.stop_loss, target),
            reason_codes=("ema_cross", f"{direction.value.lower()}_entry"),
            market_context={
                "bars": len(history),
                "last_close": str(last.close),
                "quote_as_of": last.close_time.isoformat(),
                "strategy_reason": intent.reason,
            },
            created_at=now,
            expires_at=now + self._ttl,
        )

    # ----------------------------------------------------------------- reads

    def list_signals(self) -> tuple[Signal, ...]:
        """All signals, after expiring the stale ones."""
        self._expire()
        return self._store.list()

    def get(self, signal_id: str) -> Signal:
        self._expire()
        signal = self._store.get(signal_id)
        if signal is None:
            raise SignalNotFoundError(signal_id)
        return signal

    def risk_preview(self, signal: Signal) -> RiskDecision:
        """Dry-run the gate for a signal without converting it."""
        return self._preview(signal, self._clock())

    # ---------------------------------------------------------------- actions

    def convert(self, signal_id: str) -> ConversionResult:
        """Produce a **draft** intent, re-running the gate on the way."""
        signal = self.get(signal_id)
        now = self._clock()

        if signal.is_expired(now):
            self._store.update(signal.with_status(SignalStatus.EXPIRED))
            raise SignalStateError("signal expired before it could be converted")
        if signal.status is SignalStatus.IGNORED:
            raise SignalStateError("an ignored signal cannot be converted")
        if signal.status is SignalStatus.CONVERTED:
            raise SignalStateError("signal was already converted")
        if self._holds_direction(signal.symbol, signal.direction.side):
            # Same rule as the runner: an open position in this direction must be
            # closed by its stop/target before the idea may be entered again.
            raise SignalPositionOpenError(signal.symbol)

        intent = self._intent_for(signal, now)
        decision = self._gate.evaluate(
            intent, self._ledger.snapshot(now=now), self._market_context(signal)
        )
        if not decision.approved:
            self._store.update(signal.with_status(SignalStatus.BLOCKED_BY_RISK, decision.reasons))
            raise SignalRiskRejectedError(decision.reasons)

        converted = signal.with_status(SignalStatus.CONVERTED)
        self._store.update(converted)
        return ConversionResult(signal=converted, intent=intent, decision=decision)

    def _holds_direction(self, symbol: str, side: Side) -> bool:
        """True when ``symbol`` already holds a non-flat position on ``side``."""
        position = self._ledger.position(symbol)
        if position.is_flat:
            return False
        held = Side.BUY if position.qty > 0 else Side.SELL
        return held is side

    def ignore(self, signal_id: str) -> Signal:
        signal = self.get(signal_id)
        if signal.status is SignalStatus.CONVERTED:
            raise SignalStateError("a converted signal cannot be ignored")
        if signal.status is SignalStatus.IGNORED:
            return signal
        ignored = signal.with_status(SignalStatus.IGNORED)
        self._store.update(ignored)
        return ignored

    # -------------------------------------------------------------- internals

    def _preview(self, signal: Signal, now: datetime) -> RiskDecision:
        try:
            intent = self._intent_for(signal, now)
        except SignalStateError as exc:
            return RiskDecision.reject(f"unmeasurable_signal:{exc}")
        return self._gate.evaluate(
            intent, self._ledger.snapshot(now=now), self._market_context(signal)
        )

    def _intent_for(self, signal: Signal, now: datetime) -> OrderIntent:
        """Size the idea to the per-trade risk budget and wrap it as an intent.

        Sizing is fixed-fractional: the loss at the stop is exactly the per-trade risk
        budget. That notional may need more cash than the account holds, so the
        leverage is *derived* from it (the smallest multiple that fits, never above the
        cap) rather than configured per trade. A wide stop needs no leverage; a tight
        one does.
        """
        risk_per_unit = abs(signal.entry_ref - signal.stop_loss)
        if risk_per_unit <= 0:
            raise SignalStateError("stop equals entry: risk cannot be measured")

        state = self._ledger.snapshot(now=now)
        risk_pct = self._gate.limits.max_risk_per_trade_pct
        max_leverage = min(int(self._gate.limits.max_leverage), MAX_LEVERAGE)
        leverage = choose_leverage(
            equity=state.equity,
            entry=signal.entry_ref,
            stop_loss=signal.stop_loss,
            risk_pct=risk_pct,
            max_leverage=max_leverage,
        )
        capacity = state.equity * risk_pct / _HUNDRED
        # Two independent ceilings: the risk budget (never risk more) and the margin
        # available at this leverage (never post more collateral than exists).
        qty_for_risk = capacity / risk_per_unit
        qty_for_margin = state.equity * Decimal(leverage) * MARGIN_UTILISATION / signal.entry_ref
        # Round *down*: a quantity rounded up would make the order exceed the very
        # cap it was sized from, and the gate would reject its own sizing.
        qty = min(qty_for_risk, qty_for_margin).quantize(_QUANTUM, rounding=ROUND_DOWN)
        if qty <= 0:
            raise SignalStateError("risk budget leaves no room for this signal")

        return OrderIntent(
            symbol=signal.symbol,
            side=signal.direction.side,
            order_type=OrderType.MARKET,
            qty=qty,
            price=signal.entry_ref,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            strategy_id=signal.strategy_id,
            reason=f"signal {signal.signal_id} ({signal.direction.value})",
            leverage=leverage,
            position_side=PositionSide.for_entry(signal.direction.side),
            created_at=now,
        )

    def _market_context(self, signal: Signal) -> MarketContext:
        """Quote the signal was derived from, with the time it was observed."""
        raw = signal.market_context.get("quote_as_of")
        if not isinstance(raw, str):
            raise SignalStateError("signal has no market context timestamp")
        observed = datetime.fromisoformat(raw)
        return MarketContext(
            quotes=(
                MarketQuote(
                    symbol=signal.symbol,
                    price=signal.entry_ref,
                    as_of=observed,
                ),
            ),
            now=self._clock(),
        )

    def _expire(self) -> None:
        now = self._clock()
        for signal in self._store.list():
            if signal.status in (SignalStatus.CONVERTED, SignalStatus.IGNORED):
                continue
            if signal.is_expired(now) and signal.status is not SignalStatus.EXPIRED:
                self._store.update(signal.with_status(SignalStatus.EXPIRED))
