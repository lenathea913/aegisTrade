"""Closing positions when their stop or target is touched.

Rules, in order:

1. Only open positions **with** protection are watched. A position without a stop
   cannot be monitored into safety, so it is reported and left alone rather than
   closing on a guess.
2. The trigger uses the latest closed price: ``mark <= stop`` for a long,
   ``mark >= stop`` for a short; the target mirrors it.
3. The exit is an ordinary ``OrderIntent`` (MARKET, opposite side, the position's
   full quantity) whose stop sits just beyond the current mark, so the intent is
   internally valid and the gate can measure it. It goes through ``TradingPipeline``,
   so a rejection is recorded in the audit trail exactly like any other rejection.
4. A position that is already flat is skipped: the ledger drops its protection when
   the position closes, so this is only a guard against a race in the caller.
5. Before the intent is built, the venue is asked whether it already closed (or
    reduced) the leg and its protection order is gone. If so the exit is *withheld*:
    the venue executed first, and booking that fill is the reconciliation's job — see
    ``position_monitor.venue``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from aegis_core import (
    MarketContext,
    MarketQuote,
    OrderIntent,
    OrderType,
    Position,
    PositionSide,
    Side,
    utc_now,
)
from aegis_execution import ExchangeError, PipelineResult, TradingPipeline
from aegis_ledger import PortfolioLedger, PositionProtection
from aegis_observability import get_logger
from position_monitor.quotes import QuoteSource
from position_monitor.venue import VenueCheck

__all__ = ["ExitTrigger", "MonitorAction", "PositionMonitor"]

#: Distance between the mark and the stop attached to a closing intent (0.2 %).
#: It only has to be non-zero: the intent is closing, not opening, exposure.
_STOP_BUFFER = Decimal("0.002")

#: Quantities are venue-rounded; a difference below this is the same position.
_QTY_EPSILON = Decimal("0.000000001")


class ExitTrigger(StrEnum):
    """Why the monitor decided to close."""

    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


@dataclass(frozen=True)
class MonitorAction:
    """One attempt to close a position, with the pipeline's verdict."""

    symbol: str
    trigger: ExitTrigger
    mark_price: Decimal
    intent: OrderIntent
    result: PipelineResult
    level: Decimal

    @property
    def closed(self) -> bool:
        """True only when the gate approved *and* the broker filled the exit."""
        return self.result.filled


class PositionMonitor:
    """Watch protected positions and close them when a level is touched."""

    def __init__(
        self,
        *,
        ledger: PortfolioLedger,
        pipeline: TradingPipeline,
        quotes: QuoteSource,
        clock: Callable[[], datetime] = utc_now,
        strategy_id: str = "position_monitor",
        protect_take_profit: bool = True,
        use_live_price: bool = False,
        live_quotes: QuoteSource | None = None,
        venue_check: VenueCheck | None = None,
        on_venue_execution: Callable[[str], None] | None = None,
    ) -> None:
        self._ledger = ledger
        self._pipeline = pipeline
        self._quotes = quotes
        self._clock = clock
        self._strategy_id = strategy_id
        self._protect_take_profit = protect_take_profit
        self._use_live_price = use_live_price
        self._live_quotes = live_quotes
        #: Answers "did the venue already close this leg?" before an exit is sent.
        self._venue_check = venue_check
        #: Called when an exit is withheld, so the fill can be booked immediately.
        self._on_venue_execution = on_venue_execution
        self._logger = get_logger("aegis.position_monitor")

    @property
    def ledger(self) -> PortfolioLedger:
        """The book this monitor watches (restored by the CLI at startup)."""
        return self._ledger

    def check(self) -> tuple[MonitorAction, ...]:
        """Evaluate every protected position once; return the exit attempts."""
        actions: list[MonitorAction] = []
        for protection in self._ledger.protections:
            action = self._check_one(protection)
            if action is not None:
                actions.append(action)
        return tuple(actions)

    # ------------------------------------------------------------- internals

    def _check_one(self, protection: PositionProtection) -> MonitorAction | None:
        position = self._ledger.position(protection.symbol)
        if position.is_flat:
            return None

        quote = self._quote(protection.symbol)
        if quote is None:
            self._logger.warning(
                "monitor_no_quote", symbol=protection.symbol, reason="no candle stored"
            )
            return None

        trigger, level = self._trigger(protection, quote.price)
        if trigger is None:
            return None

        if self._venue_executed_first(protection, position):
            return None

        now = self._clock()
        intent = self._exit_intent(protection, position, quote, trigger, now)
        market = MarketContext(
            quotes=(MarketQuote(symbol=quote.symbol, price=quote.price, as_of=quote.as_of),),
            now=now,
        )
        result = self._pipeline.process(intent, market)

        self._logger.info(
            "monitor_exit_sent",
            symbol=protection.symbol,
            trigger=trigger.value,
            mark=str(quote.price),
            filled=result.filled,
            approved=result.decision.approved,
        )
        if not result.decision.approved:
            self._logger.warning(
                "monitor_exit_rejected",
                symbol=protection.symbol,
                reasons=list(result.reasons),
            )
        return MonitorAction(
            symbol=protection.symbol,
            trigger=trigger,
            mark_price=quote.price,
            intent=intent,
            result=result,
            level=level,
        )

    def _venue_executed_first(self, protection: PositionProtection, position: Position) -> bool:
        """True when the venue already closed (or reduced) the leg we wanted to exit.

        Withholding the exit here is what keeps one stop from becoming two: the venue
        executed, so the only remaining work is to *record* that execution. An
        unreachable venue answers nothing and the exit proceeds — the broker would fail
        the submission anyway, and guessing "already closed" would leave a position
        unprotected.
        """
        if self._venue_check is None or protection.side is None:
            return False
        ids = tuple(
            order_id
            for order_id in (position.stop_order_id, position.tp_order_id)
            if order_id is not None
        )
        try:
            state = self._venue_check.inspect(
                protection.symbol, side=protection.side, protection_ids=ids
            )
        except ExchangeError as exc:
            self._logger.warning(
                "monitor_venue_check_failed", symbol=protection.symbol, error=str(exc)
            )
            return False
        if state.protection_open or state.venue_qty >= position.abs_qty - _QTY_EPSILON:
            return False

        self._logger.warning(
            "monitor_exit_skipped_venue_executed",
            symbol=protection.symbol,
            ledger_qty=str(position.abs_qty),
            venue_qty=str(state.venue_qty),
            stop_order_id=position.stop_order_id,
            tp_order_id=position.tp_order_id,
            detail="the venue already closed part or all of this leg; booking its fill",
        )
        if self._on_venue_execution is not None:
            try:
                self._on_venue_execution(protection.symbol)
            except Exception as exc:  # a failed bookkeeping pass must not stop the monitor
                self._logger.error(
                    "monitor_venue_reconcile_failed",
                    symbol=protection.symbol,
                    error=str(exc),
                )
        return True

    def _quote(self, symbol: str) -> MarketQuote | None:
        """Latest price: the ledger's live mark when configured, else the last candle.

        ``position_monitor.use_live_price`` is the switch for acting on a real-time
        price instead of waiting for the next closed candle. ``live_quotes`` (Redis
        marks from ``apps/market_streamer``) wins, then a mark pushed into the ledger,
        and the last closed candle is the honest fallback when neither exists.
        """
        if self._use_live_price:
            if self._live_quotes is not None:
                live = self._live_quotes.latest(symbol)
                if live is not None:
                    return live
            mark = self._ledger.mark(symbol)
            if mark is not None:
                return MarketQuote(symbol=symbol, price=mark, as_of=self._clock())
        return self._quotes.latest(symbol)

    def _trigger(
        self, protection: PositionProtection, mark: Decimal
    ) -> tuple[ExitTrigger | None, Decimal]:
        """Return the trigger (with its level) that ``mark`` has crossed, if any."""
        if protection.side is Side.BUY:
            if mark <= protection.stop_loss:
                return ExitTrigger.STOP_LOSS, protection.stop_loss
            target = protection.take_profit
            if self._protect_take_profit and target is not None and mark >= target:
                return ExitTrigger.TAKE_PROFIT, target
            return None, protection.stop_loss

        if mark >= protection.stop_loss:
            return ExitTrigger.STOP_LOSS, protection.stop_loss
        target = protection.take_profit
        if self._protect_take_profit and target is not None and mark <= target:
            return ExitTrigger.TAKE_PROFIT, target
        return None, protection.stop_loss

    def _exit_intent(
        self,
        protection: PositionProtection,
        position: Position,
        quote: MarketQuote,
        trigger: ExitTrigger,
        now: datetime,
    ) -> OrderIntent:
        """Build the closing intent; the stop sits just beyond the mark."""
        closing_side = Side.SELL if protection.side is Side.BUY else Side.BUY
        offset = quote.price * _STOP_BUFFER
        stop_loss = quote.price + offset if closing_side is Side.SELL else quote.price - offset
        level = protection.stop_loss if trigger is ExitTrigger.STOP_LOSS else protection.take_profit

        return OrderIntent(
            symbol=protection.symbol,
            side=closing_side,
            order_type=OrderType.MARKET,
            qty=position.abs_qty,
            price=quote.price,
            stop_loss=stop_loss,
            take_profit=None,
            strategy_id=self._strategy_id,
            reason=f"position_monitor: {trigger.value} touched at {quote.price} (level {level})",
            # The exit belongs to the leg it closes: on futures hedge mode a SELL that
            # closes a long must still be addressed to the LONG leg. Leverage is the
            # leg's, so the closing trade is booked against the same margin.
            leverage=position.leverage,
            position_side=PositionSide.for_entry(protection.side),
            created_at=now,
        )
