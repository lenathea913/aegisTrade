"""Read-side service behind the API.

The service answers four questions and never mutates anything:

* ``health`` — is the process up, is the database reachable, is the kill switch on;
* ``positions`` — what exposure does the ledger hold right now, marked;
* ``risk_budget`` — how much of each configured limit is left;
* ``journal`` — recent trades and audit events, merged and newest first.

Every number comes from the ledger or the database; the API computes no PnL of its
own and never writes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from aegis_config import Settings
from aegis_core import Trade, __version__, utc_now
from aegis_ledger import AuditEvent, PortfolioLedger
from api.repository import ApiRepository
from api.schemas import (
    HealthResponse,
    JournalEntryResponse,
    JournalResponse,
    LimitsResponse,
    PositionResponse,
    PositionsResponse,
    RiskBudgetResponse,
)

__all__ = ["ApiService"]

_HUNDRED = Decimal("100")
DEFAULT_JOURNAL_LIMIT = 50
# The dashboard polls every 5 s; with the database down, each ping costs the
# connect timeout. Cache the answer so a dead database slows one poll a minute,
# not every poll.
HEALTH_CACHE_SECONDS = 30.0


class ApiService:
    """Composes the ledger, the repository and the configuration."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: ApiRepository,
        ledger: PortfolioLedger,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._ledger = ledger
        self._clock = clock
        self._health_cache: tuple[float, HealthResponse] | None = None

    @property
    def ledger(self) -> PortfolioLedger:
        """The book this service reports (restored from the database at startup)."""
        return self._ledger

    # --------------------------------------------------------------- health

    def health(self) -> HealthResponse:
        """Process, database and kill-switch status, probed at most every 30 s."""
        monotonic_now = time.monotonic()
        if (
            self._health_cache is not None
            and monotonic_now - self._health_cache[0] < HEALTH_CACHE_SECONDS
        ):
            return self._health_cache[1]
        database_ok = self._repository.ping()
        response = HealthResponse(
            status="ok" if database_ok else "degraded",
            version=__version__,
            environment=self._settings.environment.value,
            execution_mode=self._settings.execution.mode.value,
            database="ok" if database_ok else "unavailable",
            kill_switch=self._settings.risk.kill_switch,
            time=self._clock(),
        )
        self._health_cache = (monotonic_now, response)
        return response

    # ------------------------------------------------------------ positions

    def positions(self) -> PositionsResponse:
        state = self._ledger.snapshot(now=self._clock())
        positions = [
            PositionResponse(
                symbol=position.symbol,
                side=None if position.side is None else position.side.value,
                qty=str(position.qty),
                avg_entry=str(position.avg_entry),
                notional=str(position.notional),
                unrealized_pnl=str(position.unrealized_pnl),
                realized_pnl=str(position.realized_pnl),
            )
            for position in state.open_positions
        ]
        return PositionsResponse(count=len(positions), positions=positions)

    # ---------------------------------------------------------- risk budget

    def risk_budget(self) -> RiskBudgetResponse:
        state = self._ledger.snapshot(now=self._clock())
        limits = self._settings.risk.limits
        equity = state.equity

        daily_limit = equity * limits.max_daily_loss_pct / _HUNDRED
        weekly_limit = equity * limits.max_weekly_loss_pct / _HUNDRED
        daily_used = max(-state.daily_realized_pnl, Decimal("0"))
        weekly_used = max(-state.weekly_realized_pnl, Decimal("0"))

        exposure = sum(
            (
                abs(position.qty) * (self._ledger.mark(position.symbol) or position.avg_entry)
                for position in state.open_positions
            ),
            Decimal("0"),
        )
        max_total_exposure = equity * limits.max_total_exposure_pct / _HUNDRED

        return RiskBudgetResponse(
            equity=str(equity),
            risk_per_trade_capacity=str(equity * limits.max_risk_per_trade_pct / _HUNDRED),
            daily_realized_pnl=str(state.daily_realized_pnl),
            daily_loss_limit=str(daily_limit),
            daily_loss_remaining=str(max(daily_limit - daily_used, Decimal("0"))),
            weekly_realized_pnl=str(state.weekly_realized_pnl),
            weekly_loss_limit=str(weekly_limit),
            weekly_loss_remaining=str(max(weekly_limit - weekly_used, Decimal("0"))),
            total_exposure=str(exposure),
            total_exposure_pct=str(exposure / equity * _HUNDRED),
            max_total_exposure=str(max_total_exposure),
            leverage=str(exposure / equity),
            open_positions=len(state.open_positions),
            kill_switch=self._settings.risk.kill_switch,
            limits=LimitsResponse(
                max_risk_per_trade_pct=str(limits.max_risk_per_trade_pct),
                max_daily_loss_pct=str(limits.max_daily_loss_pct),
                max_weekly_loss_pct=str(limits.max_weekly_loss_pct),
                max_exposure_per_symbol_pct=str(limits.max_exposure_per_symbol_pct),
                max_total_exposure_pct=str(limits.max_total_exposure_pct),
                max_leverage=str(limits.max_leverage),
                allowed_symbols=sorted(limits.allowed_symbols),
            ),
        )

    # -------------------------------------------------------------- journal

    def journal(self, limit: int = DEFAULT_JOURNAL_LIMIT) -> JournalResponse:
        trades = self._repository.trades(limit)
        events = self._repository.audit_events(limit)

        merged = [_trade_entry(trade) for trade in trades]
        merged.extend(_audit_entry(event) for event in events)
        merged.sort(key=lambda entry: entry.at, reverse=True)

        entries = merged[:limit]
        return JournalResponse(count=len(entries), limit=limit, entries=entries)


def _trade_entry(trade: Trade) -> JournalEntryResponse:
    return JournalEntryResponse(
        kind="trade",
        at=trade.filled_at,
        title=f"{trade.side.value} {trade.qty} {trade.symbol} @ {trade.price}",
        detail={
            "trade_id": trade.trade_id,
            "order_id": trade.order_id,
            "fee": str(trade.fee),
            "funding": str(trade.funding),
            "strategy_id": trade.strategy_id,
            "r_multiple": None if trade.r_multiple is None else str(trade.r_multiple),
        },
    )


def _audit_entry(event: AuditEvent) -> JournalEntryResponse:
    return JournalEntryResponse(
        kind="audit",
        at=event.created_at,
        title=event.event_type.value,
        detail={
            "actor": event.actor,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "payload": event.payload,
        },
    )
