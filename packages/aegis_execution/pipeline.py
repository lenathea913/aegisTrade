"""Risk-first trading pipeline.

One method, one order of operations, no shortcuts:

    OrderIntent -> RiskGate -> ApprovedOrder -> ExecutionGateway -> fill
                -> PortfolioLedger -> audit events (+ optional persistence)

This is the only place that walks the whole path, so the guarantee "no order
bypasses the risk engine" has exactly one object to test and every service (API,
scheduler) calls it instead of hand-wiring the pieces. If the gate rejects, the
gateway is never constructed for that intent and no order exists anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from aegis_core import (
    ApprovedOrder,
    MarketContext,
    Order,
    OrderIntent,
    RiskDecision,
    Trade,
)
from aegis_execution.errors import ExecutionError
from aegis_execution.gateway import ExecutionGateway, ExecutionResult
from aegis_ledger import (
    AuditEvent,
    EventType,
    LedgerUpdate,
    LedgerWriter,
    PortfolioLedger,
    PositionProtection,
)
from aegis_risk import RiskGate

__all__ = ["PipelineResult", "TradingPipeline"]


@dataclass(frozen=True)
class PipelineResult:
    """Everything one intent produced, including the audit trail."""

    intent: OrderIntent
    decision: RiskDecision
    approved: ApprovedOrder | None = None
    execution: ExecutionResult | None = None
    update: LedgerUpdate | None = None
    events: tuple[AuditEvent, ...] = ()

    @property
    def approved_order(self) -> ApprovedOrder | None:
        return self.approved

    @property
    def trade(self) -> Trade | None:
        """The executed ``Trade``, or ``None`` when nothing filled."""
        return None if self.execution is None else self.execution.trade

    @property
    def filled(self) -> bool:
        return self.execution is not None and self.execution.filled

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.decision.reasons


class TradingPipeline:
    """Gate, gateway and ledger bound together for one trading loop."""

    def __init__(
        self,
        *,
        gate: RiskGate,
        gateway: ExecutionGateway,
        ledger: PortfolioLedger,
        writer: LedgerWriter | None = None,
    ) -> None:
        self._gate = gate
        self._gateway = gateway
        self._ledger = ledger
        self._writer = writer

    @property
    def ledger(self) -> PortfolioLedger:
        return self._ledger

    def process(self, intent: OrderIntent, market: MarketContext) -> PipelineResult:
        """Evaluate, execute and record one intent."""
        state = self._ledger.snapshot(now=market.now)
        decision = self._gate.evaluate(intent, state, market)

        if not decision.approved:
            event = AuditEvent(
                event_type=EventType.RISK_REJECTED,
                entity_type="order_intent",
                entity_id=intent.symbol,
                payload={
                    "symbol": intent.symbol,
                    "strategy_id": intent.strategy_id,
                    "side": intent.side.value,
                    "qty": str(intent.qty),
                    "reasons": list(decision.reasons),
                },
            )
            self._emit(event)
            return PipelineResult(intent=intent, decision=decision, events=(event,))

        if market.now < intent.created_at:
            raise ExecutionError(
                "market context is older than the intent; refusing to trade on stale time"
            )

        approved = ApprovedOrder(
            intent=intent,
            risk_checks_passed=decision.reasons,
            max_risk_amount=decision.max_risk_amount,
            approved_at=market.now,
        )
        events: list[AuditEvent] = [
            AuditEvent(
                event_type=EventType.ORDER_APPROVED,
                entity_type="order",
                entity_id=approved.approval_id,
                payload={
                    "approval_id": approved.approval_id,
                    "symbol": approved.symbol,
                    "side": approved.side.value,
                    "qty": str(approved.qty),
                    "max_risk_amount": str(approved.max_risk_amount),
                    "risk_checks": list(approved.risk_checks_passed),
                },
            )
        ]

        execution = self._gateway.execute(approved, market=market)
        order = execution.order

        if execution.trade is None:
            events.append(
                AuditEvent(
                    event_type=EventType.ORDER_NOT_FILLED,
                    entity_type="order",
                    entity_id=order.order_id,
                    payload={
                        "order_id": order.order_id,
                        "order_type": order.order_type.value,
                        "limit_price": _text(order.price),
                        "market_price": _text(market.price(order.symbol)),
                    },
                )
            )
            self._persist_order(order, approved)
            self._emit(*events)
            return PipelineResult(
                intent=intent,
                decision=decision,
                approved=approved,
                execution=execution,
                events=tuple(events),
            )

        trade = execution.trade
        events.append(
            AuditEvent(
                event_type=EventType.ORDER_SUBMITTED,
                entity_type="order",
                entity_id=order.order_id,
                payload={
                    "order_id": order.order_id,
                    "exchange_order_id": order.exchange_order_id,
                    "filled_qty": str(order.filled_qty),
                    "avg_fill_price": _text(order.avg_fill_price),
                    "broker": order.broker,
                },
            )
        )

        update = self._ledger.apply_trade(trade)
        if not update.position.is_flat and update.position.side is not None:
            # The approved levels travel with the position, so anything watching the
            # ledger (e.g. the position monitor) knows where the exit belongs.
            self._ledger.set_protection(
                PositionProtection(
                    symbol=update.position.symbol,
                    side=update.position.side,
                    qty=update.position.abs_qty,
                    stop_loss=approved.stop_loss,
                    take_profit=approved.take_profit,
                    strategy_id=approved.strategy_id,
                )
            )
        events.append(
            AuditEvent(
                event_type=EventType.TRADE_RECORDED,
                entity_type="trade",
                entity_id=trade.trade_id,
                payload={
                    "trade_id": trade.trade_id,
                    "order_id": trade.order_id,
                    "symbol": trade.symbol,
                    "side": trade.side.value,
                    "qty": str(trade.qty),
                    "price": str(trade.price),
                    "fee": str(trade.fee),
                    "realized_delta": str(update.realized_delta),
                },
            )
        )
        events.append(
            AuditEvent(
                event_type=EventType.POSITION_UPDATED,
                entity_type="position",
                entity_id=update.position.symbol,
                payload={
                    "symbol": update.position.symbol,
                    "qty": str(update.position.qty),
                    "avg_entry": str(update.position.avg_entry),
                    "realized_pnl": str(update.position.realized_pnl),
                    "equity": str(update.equity),
                },
            )
        )

        self._persist_order(order, approved)
        if self._writer is not None:
            self._writer.save_trade(trade)
            self._writer.save_position(update.position)
        self._emit(*events)

        return PipelineResult(
            intent=intent,
            decision=decision,
            approved=approved,
            execution=execution,
            update=update,
            events=tuple(events),
        )

    # ------------------------------------------------------------- helpers

    def _persist_order(self, order: Order, approved: ApprovedOrder) -> None:
        if self._writer is not None:
            self._writer.save_order(order, approved)

    def _emit(self, *events: AuditEvent) -> None:
        if self._writer is None:
            return
        for event in events:
            self._writer.append_audit(event)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
