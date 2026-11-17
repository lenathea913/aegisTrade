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

from dataclasses import dataclass, replace
from decimal import Decimal

from aegis_alerting import Alert, Alerter, AlertLevel
from aegis_core import (
    ApprovedOrder,
    MarketContext,
    Order,
    OrderIntent,
    Position,
    PositionSide,
    RiskDecision,
    Trade,
    get_random_motivation,
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
from aegis_observability import get_logger
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
    #: Set when the exchange could not be reached or refused the order. The ledger
    #: is untouched in that case: no fill, no position, no trade.
    error: str | None = None

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
        alerter: Alerter | None = None,
    ) -> None:
        self._gate = gate
        self._alerter = alerter
        self._gateway = gateway
        self._ledger = ledger
        self._writer = writer
        self._logger = get_logger("aegis.execution")

    @property
    def ledger(self) -> PortfolioLedger:
        return self._ledger

    def process(self, intent: OrderIntent, market: MarketContext) -> PipelineResult:
        """Evaluate, execute and record one intent."""
        state = self._ledger.snapshot(now=market.now)
        # The gate owns risk rejections (kill switch, daily loss included).
        decision = self._gate.evaluate(intent, state, market, alerter=self._alerter)

        if not decision.approved:
            # A rejection is the gate doing its job. Log it at INFO with a next step,
            # so an operator reading the tail sees advice and not only a code.
            self._logger.info(
                "order_rejected",
                symbol=intent.symbol,
                strategy_id=intent.strategy_id,
                side=intent.side.value,
                reason_count=len(decision.reasons),
                motivation=get_random_motivation(),
            )
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

        try:
            execution = self._gateway.execute(approved, market=market)
        except ExecutionError as exc:
            # A broker/exchange failure is not a fill: record it, tell the operator,
            # and leave the ledger untouched rather than half-updated.
            self._logger.error(
                "exchange_error",
                symbol=intent.symbol,
                strategy_id=intent.strategy_id,
                side=intent.side.value,
                error=str(exc),
                motivation=get_random_motivation(),
            )
            failure = AuditEvent(
                event_type=EventType.ORDER_NOT_FILLED,
                entity_type="order",
                entity_id=approved.approval_id,
                payload={
                    "approval_id": approved.approval_id,
                    "symbol": intent.symbol,
                    "reason": "exchange_error",
                    "error": str(exc),
                },
            )
            events.append(failure)
            self._notify(
                Alert(
                    level=AlertLevel.CRITICAL,
                    title="Order rejected by exchange",
                    message=f"The venue refused or could not take the order: {exc}",
                    source="execution",
                    context={
                        "symbol": intent.symbol,
                        "side": intent.side.value,
                        "status": "REJECTED",
                        "price": str(market.price(intent.symbol)),
                        "reason": "exchange_error",
                    },
                )
            )
            # The approval is part of the record even though no fill followed it.
            self._emit(*events)
            return PipelineResult(
                intent=intent,
                decision=decision,
                approved=approved,
                events=tuple(events),
                error=str(exc),
            )
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
        protected = False
        if not update.position.is_flat and update.position.side is not None:
            # The approved levels travel with the position *and* with the row it is
            # persisted as, so anything watching the ledger (the position monitor) and
            # anything reading the database (a restarted process) sees the same stop.
            position = self._ledger.set_protection(
                PositionProtection(
                    symbol=update.position.symbol,
                    side=update.position.side,
                    qty=update.position.abs_qty,
                    stop_loss=approved.stop_loss,
                    take_profit=approved.take_profit,
                    strategy_id=approved.strategy_id,
                )
            )
            update = replace(update, position=position)
            protected = True
        else:
            # The leg is gone, so anything the venue holds for it must go with it.
            self._release_venue_protection(update.position.symbol)
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

        if protected:
            events.extend(self._place_venue_protection(order, approved, update.position))

        self._persist_order(order, approved)
        if self._writer is not None:
            self._writer.save_trade(trade)
            self._writer.save_position(update.position)
        self._emit(*events)
        self._notify(
            Alert(
                level=AlertLevel.INFO,
                title="Order filled",
                message=f"{trade.side.value} {trade.qty} {trade.symbol} at {trade.price}.",
                source="execution",
                context={
                    "symbol": trade.symbol,
                    "side": trade.side.value,
                    "status": "FILLED",
                    "price": str(trade.price),
                    "reason": intent.strategy_id,
                },
            )
        )

        return PipelineResult(
            intent=intent,
            decision=decision,
            approved=approved,
            execution=execution,
            update=update,
            events=tuple(events),
        )

    # ------------------------------------------------------------- helpers

    def _place_venue_protection(
        self, order: Order, approved: ApprovedOrder, position: Position
    ) -> list[AuditEvent]:
        """Attach the stop and target on the venue so they outlive this process.

        A failure here does not undo the fill — the position exists either way — but it
        is a CRITICAL alert and an audit row: a leveraged position whose stop only
        lives in memory is exactly the risk this path exists to remove.
        """
        if not self._gateway.places_protection:
            return []
        side = position.side
        if side is None:  # pragma: no cover - caller checks is_flat
            return []
        try:
            placed = self._gateway.protect(
                symbol=position.symbol,
                side=side.opposite,
                position_side=order.position_side or PositionSide.for_entry(side),
                qty=position.abs_qty,
                stop_loss=approved.stop_loss,
                take_profit=approved.take_profit,
            )
        except ExecutionError as exc:
            self._logger.error(
                "protection_not_placed",
                symbol=position.symbol,
                error=str(exc),
                motivation=get_random_motivation(),
            )
            self._notify(
                Alert(
                    level=AlertLevel.CRITICAL,
                    title="Stop not placed on the venue",
                    message=(
                        f"{position.symbol} is open but the venue rejected its stop; "
                        "only the local monitor protects it."
                    ),
                    source="execution",
                    context={
                        "symbol": position.symbol,
                        "status": "UNPROTECTED",
                        "detail": str(exc)[:400],
                    },
                )
            )
            return [
                AuditEvent(
                    event_type=EventType.PROTECTION_FAILED,
                    entity_type="position",
                    entity_id=position.symbol,
                    payload={"symbol": position.symbol, "error": str(exc)},
                )
            ]
        if placed is None or not placed.any_placed:  # pragma: no cover - defensive
            return []
        self._logger.info(
            "protection_placed",
            symbol=position.symbol,
            stop_order_id=placed.stop_order_id,
            take_order_id=placed.take_order_id,
        )
        return [
            AuditEvent(
                event_type=EventType.PROTECTION_PLACED,
                entity_type="position",
                entity_id=position.symbol,
                payload={
                    "symbol": position.symbol,
                    "stop_order_id": placed.stop_order_id,
                    "take_order_id": placed.take_order_id,
                    "stop_loss": str(approved.stop_loss),
                    "take_profit": _text(approved.take_profit),
                },
            )
        ]

    def _release_venue_protection(self, symbol: str) -> None:
        """Cancel venue-side exits after the leg closed; never fails the close."""
        try:
            cancelled = self._gateway.cancel_protection(symbol)
        except ExecutionError as exc:
            self._logger.warning("protection_cancel_failed", symbol=symbol, error=str(exc))
            return
        if cancelled:
            self._logger.info("protection_cancelled", symbol=symbol, count=cancelled)

    def _persist_order(self, order: Order, approved: ApprovedOrder) -> None:
        if self._writer is not None:
            self._writer.save_order(order, approved)

    def _notify(self, alert: Alert) -> None:
        if self._alerter is not None:
            self._alerter.send(alert)

    def _emit(self, *events: AuditEvent) -> None:
        if self._writer is None:
            return
        for event in events:
            self._writer.append_audit(event)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
