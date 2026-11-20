"""The risk gate.

``evaluate`` is the single place where an ``OrderIntent`` becomes (or fails to
become) an ``ApprovedOrder``. Design choices worth knowing:

* **All rules run.** A rejection lists every violation instead of stopping at the
  first one, so one audit row explains the whole decision.
* **Percent means percent.** Limits are compared as ``equity * pct / 100``.
* **A cap may be touched; a loss floor may not be reached.** Exposure/risk caps
  reject only when strictly exceeded, loss limits reject on reaching them —
  touching the daily loss limit is already the signal to stop.
* **No price, no approval.** A MARKET intent with no reference price and no fresh
  quote cannot be measured, so it is rejected rather than guessed.
* **The kill switch is an argument, not a config read**, and ``RiskGate`` binds it
  once so a caller cannot forget it.
* **A new idea needs a free slot.** ``max_open_positions`` is counted from the
  book, not from configuration intent, so a scan across ten symbols cannot turn
  into ten concurrent bets; a position-reducing order never needs a slot.
* **A protective exit is judged, not blocked.** An intent that only reduces an
  existing position (opposite side, quantity not exceeding it) skips the caps that
  exist to limit *new* exposure — risk-per-trade, loss windows, exposure and
  leverage. Otherwise a daily-loss halt would trap an open position and turn a
  bounded loss into an unbounded one. The kill switch, the symbol allow-list, the
  market-data freshness rule, the entry price and the stop-side check still apply.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from aegis_alerting import Alerter, alert_for_rejection
from aegis_config import Settings
from aegis_core import (
    MAINTENANCE_MARGIN_PCT,
    MAX_LEVERAGE,
    MarketContext,
    OrderIntent,
    PortfolioState,
    Position,
    RiskDecision,
    RiskLimit,
    Side,
    StrictModel,
    Timeframe,
    liquidation_price,
)
from aegis_risk.reasons import RejectReason, RiskCheck

__all__ = ["DEFAULT_STALE_AFTER_SECONDS", "RiskGate", "evaluate"]

DEFAULT_STALE_AFTER_SECONDS = 60
_HUNDRED = Decimal("100")


def _detail(reason: RejectReason, detail: str) -> str:
    return f"{reason.value}:{detail}"


def _mark_price(position: Position, market_context: MarketContext) -> Decimal:
    """Mark at the latest quote when available, otherwise at cost basis."""
    price = market_context.price(position.symbol)
    return position.avg_entry if price is None else price


def _total_exposure(state: PortfolioState, market_context: MarketContext) -> Decimal:
    return sum(
        (abs(position.qty) * _mark_price(position, market_context) for position in state.positions),
        Decimal("0"),
    )


def _symbol_exposure(state: PortfolioState, market_context: MarketContext, symbol: str) -> Decimal:
    position = state.position(symbol)
    return abs(position.qty) * _mark_price(position, market_context)


def _reduces_position(intent: OrderIntent, state: PortfolioState) -> bool:
    """True when ``intent`` can only shrink the current position, never open/extend."""
    position = state.position(intent.symbol)
    if position.side is None:
        return False
    return intent.side is position.side.opposite and intent.qty <= position.abs_qty


def evaluate(
    intent: OrderIntent,
    portfolio_state: PortfolioState,
    risk_limits: RiskLimit,
    market_context: MarketContext,
    *,
    kill_switch: bool = False,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> RiskDecision:
    """Judge one intent against the limits and return the verdict.

    Args:
        intent: the strategy's proposal (never executable on its own).
        portfolio_state: equity, realized PnL for the day/week and open positions.
        risk_limits: hard limits; percentages are percent units.
        market_context: quotes plus the decision timestamp.
        kill_switch: operator halt; when true nothing is ever approved.
        stale_after_seconds: maximum quote age considered usable.

    Returns:
        ``RiskDecision``. When approved, ``max_risk_amount`` is the risk the engine
        accepted (``|entry - stop| * qty``) and ``reasons`` records the checks that
        passed. When rejected, ``max_risk_amount`` is zero and ``reasons`` lists
        every violated rule.
    """
    failures: list[str] = []
    checks: list[str] = []
    reducing = _reduces_position(intent, portfolio_state)

    equity = portfolio_state.equity

    # Rule 7: kill switch.
    if kill_switch:
        failures.append(RejectReason.KILL_SWITCH_ACTIVE.value)
    else:
        checks.append(RiskCheck.KILL_SWITCH_INACTIVE.value)

    # Rule 1: symbol allow-list (empty list denies everything).
    if risk_limits.allows(intent.symbol):
        checks.append(RiskCheck.SYMBOL_ALLOWED.value)
    else:
        failures.append(_detail(RejectReason.SYMBOL_NOT_ALLOWED, intent.symbol))

    # Rule 8: market data must exist and be fresh.
    quote = market_context.quote(intent.symbol)
    if quote is None:
        failures.append(_detail(RejectReason.MISSING_MARKET_DATA, intent.symbol))
    elif not market_context.is_fresh(intent.symbol, stale_after_seconds):
        age = market_context.age_seconds(intent.symbol)
        failures.append(
            _detail(RejectReason.STALE_MARKET_DATA, f"{age:.0f}s>{stale_after_seconds}s")
        )
    else:
        checks.append(RiskCheck.MARKET_DATA_FRESH.value)

    if reducing:
        # New-risk caps do not apply to a position-reducing order (see module docstring).
        checks.append(RiskCheck.RISK_REDUCING_ORDER.value)
    else:
        # Rule 9: how many ideas the book may hold at once. Exposure caps alone would
        # let a ten-symbol universe open position after position until the total cap
        # was reached; this decides it up front, so a fourth signal is refused with a
        # reason instead of being silently sized to nearly nothing.
        open_positions = len(portfolio_state.open_positions)
        if open_positions >= risk_limits.max_open_positions:
            failures.append(
                _detail(
                    RejectReason.MAX_OPEN_POSITIONS_REACHED,
                    f"{open_positions}>={risk_limits.max_open_positions}",
                )
            )
        else:
            checks.append(RiskCheck.OPEN_POSITIONS_WITHIN_LIMIT.value)

        # Rule 3: realized loss windows. Reaching the floor is already too late.
        daily_floor = equity * risk_limits.max_daily_loss_pct / _HUNDRED
        if -portfolio_state.daily_realized_pnl >= daily_floor:
            failures.append(
                _detail(
                    RejectReason.DAILY_LOSS_LIMIT_REACHED,
                    f"{portfolio_state.daily_realized_pnl}>=-{daily_floor}",
                )
            )
        else:
            checks.append(RiskCheck.DAILY_LOSS_WITHIN_LIMIT.value)

        weekly_floor = equity * risk_limits.max_weekly_loss_pct / _HUNDRED
        if -portfolio_state.weekly_realized_pnl >= weekly_floor:
            failures.append(
                _detail(
                    RejectReason.WEEKLY_LOSS_LIMIT_REACHED,
                    f"{portfolio_state.weekly_realized_pnl}>=-{weekly_floor}",
                )
            )
        else:
            checks.append(RiskCheck.WEEKLY_LOSS_WITHIN_LIMIT.value)

    # Entry price: an explicit reference price, else the current quote.
    entry_price = intent.price if intent.price is not None else (quote.price if quote else None)
    if entry_price is None:
        failures.append(_detail(RejectReason.MISSING_ENTRY_PRICE, intent.symbol))
        return RiskDecision(approved=False, reasons=tuple(failures), max_risk_amount=Decimal("0"))

    # The stop must still be on the losing side *at the price we would enter at*.
    stop_on_wrong_side = (intent.side is Side.BUY and entry_price <= intent.stop_loss) or (
        intent.side is Side.SELL and entry_price >= intent.stop_loss
    )
    if stop_on_wrong_side:
        failures.append(
            _detail(
                RejectReason.INVALID_STOP_FOR_SIDE,
                f"entry={entry_price} stop={intent.stop_loss}",
            )
        )

    risk_amount = abs(entry_price - intent.stop_loss) * intent.qty
    notional = intent.qty * entry_price

    if reducing:
        return _finalise(failures, checks, risk_amount)

    # Futures margin rules. A spot intent (leverage 1) passes all three trivially,
    # so one code path judges both market types.
    leverage_cap = min(risk_limits.max_leverage, Decimal(MAX_LEVERAGE))
    if Decimal(intent.leverage) > leverage_cap:
        failures.append(
            _detail(
                RejectReason.LEVERAGE_CAP_EXCEEDED,
                f"{intent.leverage}x>{int(leverage_cap)}x",
            )
        )
    else:
        checks.append(RiskCheck.LEVERAGE_WITHIN_CAP.value)

    # The notional must fit inside the margin the declared leverage provides. Sizing
    # keeps a buffer (``MARGIN_UTILISATION``); the gate only enforces the hard limit.
    margin_available = equity * Decimal(intent.leverage)
    if notional > margin_available:
        failures.append(
            _detail(
                RejectReason.INSUFFICIENT_LEVERAGE,
                f"notional={notional}>{margin_available}",
            )
        )
    else:
        checks.append(RiskCheck.MARGIN_SUFFICIENT.value)

    # The stop must be hit before the liquidation price; otherwise the risk the
    # position was sized from is unreachable and an ordinary wick liquidates it.
    liquidation = liquidation_price(
        entry_price,
        intent.side,
        intent.leverage,
        maintenance_margin_pct=MAINTENANCE_MARGIN_PCT,
    )
    stop_beyond_liquidation = (
        liquidation < intent.stop_loss
        if intent.side is Side.BUY
        else liquidation > intent.stop_loss
    )
    if not stop_beyond_liquidation:
        failures.append(
            _detail(
                RejectReason.LIQUIDATION_TOO_CLOSE,
                f"liq={liquidation} stop={intent.stop_loss} leverage={intent.leverage}x",
            )
        )
    else:
        checks.append(RiskCheck.LIQUIDATION_BEYOND_STOP.value)

    # Rule 2: risk per trade.
    max_risk_amount = equity * risk_limits.max_risk_per_trade_pct / _HUNDRED
    if risk_amount <= 0:
        failures.append(RejectReason.ZERO_STOP_DISTANCE.value)
    elif risk_amount > max_risk_amount:
        failures.append(
            _detail(
                RejectReason.RISK_PER_TRADE_EXCEEDED,
                f"{risk_amount}>{max_risk_amount}",
            )
        )
    else:
        checks.append(RiskCheck.RISK_PER_TRADE_WITHIN_LIMIT.value)

    # Rule 4: total exposure after this intent.
    total_after = _total_exposure(portfolio_state, market_context) + notional
    max_total_exposure = equity * risk_limits.max_total_exposure_pct / _HUNDRED
    if total_after > max_total_exposure:
        failures.append(
            _detail(
                RejectReason.MAX_TOTAL_EXPOSURE_EXCEEDED,
                f"{total_after}>{max_total_exposure}",
            )
        )
    else:
        checks.append(RiskCheck.TOTAL_EXPOSURE_WITHIN_LIMIT.value)

    # Rule 5: exposure per symbol after this intent.
    symbol_after = _symbol_exposure(portfolio_state, market_context, intent.symbol) + notional
    max_symbol_exposure = equity * risk_limits.max_exposure_per_symbol_pct / _HUNDRED
    if symbol_after > max_symbol_exposure:
        failures.append(
            _detail(
                RejectReason.MAX_SYMBOL_EXPOSURE_EXCEEDED,
                f"{symbol_after}>{max_symbol_exposure}",
            )
        )
    else:
        checks.append(RiskCheck.SYMBOL_EXPOSURE_WITHIN_LIMIT.value)

    # Rule 6: effective leverage on gross exposure.
    effective_leverage = total_after / equity
    if effective_leverage > risk_limits.max_leverage:
        failures.append(
            _detail(
                RejectReason.MAX_LEVERAGE_EXCEEDED,
                f"{effective_leverage:.4f}>{risk_limits.max_leverage}",
            )
        )
    else:
        checks.append(RiskCheck.LEVERAGE_WITHIN_LIMIT.value)

    return _finalise(failures, checks, risk_amount)


def _finalise(failures: list[str], checks: list[str], risk_amount: Decimal) -> RiskDecision:
    if failures:
        return RiskDecision(approved=False, reasons=tuple(failures), max_risk_amount=Decimal("0"))
    return RiskDecision(approved=True, reasons=tuple(checks), max_risk_amount=risk_amount)


class RiskGate(StrictModel):
    """Limits plus the operational flags that a caller must not forget.

    Services hold one ``RiskGate`` built from configuration and call
    ``gate.evaluate(...)``; the kill switch and staleness window therefore travel
    with the limits instead of being optional arguments at each call site.
    """

    limits: RiskLimit
    kill_switch: bool = False
    stale_after_seconds: int = Field(default=DEFAULT_STALE_AFTER_SECONDS, ge=1, le=86400)

    def evaluate(
        self,
        intent: OrderIntent,
        portfolio_state: PortfolioState,
        market_context: MarketContext,
        *,
        alerter: Alerter | None = None,
    ) -> RiskDecision:
        """Judge one intent and, when an alerter is given, report a rejection.

        ``evaluate()`` stays a pure function; only this holder of the configured
        kill switch and limits owns the side effect, so the reason strings and the
        notification text cannot drift apart.
        """
        decision = evaluate(
            intent,
            portfolio_state,
            self.limits,
            market_context,
            kill_switch=self.kill_switch,
            stale_after_seconds=self.stale_after_seconds,
        )
        if alerter is not None and not decision.approved:
            alert_for_rejection(
                symbol=intent.symbol,
                side=intent.side.value,
                reasons=decision.reasons,
                alerter=alerter,
            )
        return decision

    @classmethod
    def from_settings(cls, settings: Settings, timeframe: Timeframe | None = None) -> RiskGate:
        """Build the gate from validated configuration.

        The freshness window follows the traded timeframe unless
        ``market_data.stale_after_seconds`` pins it: a bar that is exactly one hour long
        must not be judged stale the second it closes.
        """
        bar = timeframe or settings.market_data.timeframes[0]
        return cls(
            limits=settings.risk.limits,
            kill_switch=settings.risk.kill_switch,
            stale_after_seconds=settings.market_data.stale_after_for(bar),
        )
