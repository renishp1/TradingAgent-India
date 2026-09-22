"""Paper execution for approved 4C decisions.

Pipeline: 4C candidate → paper order → deterministic or configurable fill →
3B position lifecycle → MTM / exits → net P&L → append-only journal.

This module does not call a broker. Live order placement is not imported.
4C decision objects stay unexecuted; the paper journal is the execution record.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Mapping

from grow.backtest.costs import CostModel
from grow.clock import IST, Clock, FrozenClock
from grow.config import GrowConfig
from grow.decision.integration.contract import IntegratedDecision, digest_payload
from grow.errors import GrowLiveTradingDisabled, GrowSafetyError
from grow.execution.lock import assert_paper_runtime
from grow.market.session import SessionCalendar
from grow.market_data.normalized.models import AgentMarketSnapshot, DataQualityStatus
from grow.market_data.provenance import reject_mixed_market_data
from grow.market_data.snapshots.builder import gate_snapshot_quality
from grow.live_data.health import MARKET_DATA_NOT_HEALTHY, reject_unhealthy_market_data
from grow.orchestration.models import AggregateAnalysisPackage
from grow.paper.exits import ExitReason
from grow.paper.fills import (
    FillPolicy,
    FillSimulation,
    deterministic_exit_source,
    policy_from_config,
    simulate_fill,
)
from grow.paper.journal import PaperJournal
from grow.paper.ledger import PaperLedger, Position, expected_notional
from grow.paper.orders import FillStatus, PaperLifecycle, PaperOrder
from grow.paper.positions import PaperPosition, PositionRegistry, PositionState
from grow.paper.quotes import contract_id, live_snapshot_from_agent, match_contract, quote_known_at
from grow.risk.guard import RiskGuard
from grow.types import Fill, Intent, MarketBrief, Regime, Side, Symbol, TradeProposal, Venue


_TERMINAL = {
    PaperLifecycle.CLOSED.value,
    PaperLifecycle.REJECTED.value,
    PaperLifecycle.EXPIRED.value,
}


@dataclass(frozen=True)
class PaperExecutionResult:
    accepted: bool
    reason: str
    decision_id: str
    paper_order_id: str | None = None
    position_id: str | None = None
    execution_price: float | None = None
    price_source: str | None = None
    broker_order_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "decision_id": self.decision_id,
            "paper_order_id": self.paper_order_id,
            "position_id": self.position_id,
            "execution_price": self.execution_price,
            "price_source": self.price_source,
            "broker_order_calls": 0,
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
        }


class PaperExecutionEngine:
    """Consume an approved 4C decision and run it through the paper book."""

    def __init__(
        self,
        config: GrowConfig,
        *,
        clock: Clock | None = None,
        risk_secret: str | None = None,
        risk_guard: RiskGuard | None = None,
    ) -> None:
        config.assert_safe()
        assert_paper_runtime(config.execution.mode, config.execution.live_trading_enabled, config.paper.venue_id)
        if config.execution.live_trading_enabled or config.live_data.live_trading or not config.live_data.paper_mode:
            raise GrowLiveTradingDisabled("paper execution requires paper_mode and forbids live trading")
        self.config = config
        self.clock = clock or FrozenClock(datetime.now(tz=IST))
        self._risk_secret = risk_secret
        self.guard = risk_guard or RiskGuard(config, clock=self.clock, secret=risk_secret)
        self.ledger = PaperLedger(config, self.guard, clock=self.clock)
        self.calendar = SessionCalendar(config.market, clock=self.clock)
        self.costs = CostModel()
        self.policy = policy_from_config(config)
        self.positions = PositionRegistry(
            starting_cash=config.paper.starting_cash,
            clock=self.clock,
            calendar=self.calendar,
            session_close_square_off=config.live_data.session_close_square_off,
        )
        self.journal = PaperJournal()
        self.started_at = self.clock.now()
        self.session_id = "paper-" + digest_payload(
            {"started": self.started_at.isoformat(), "ruleset": config.risk.ruleset}
        )
        self._limits = _limit_tuple(config)
        self._orders: dict[str, PaperOrder] = {}
        self._by_decision: dict[str, str] = {}
        self._seen: set[str] = set()
        self._daily_loss_halt = False
        self._pnl_day = self.started_at.astimezone(IST).date()
        self._timeout_recorded = False
        self._mark_sequence = 1
        self.broker_order_calls = 0
        self.last_risk_daily_pnl: float | None = None

    def run(
        self,
        integrator,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
        book=None,
    ) -> PaperExecutionResult:
        """4C integration then paper execution. Non-candidates are journalled and not filled."""
        decision = integrator.integrate(snapshot=snapshot, package=package, book=book)
        return self.execute(decision, snapshot, package=package)

    def execute(
        self,
        decision: IntegratedDecision,
        snapshot: AgentMarketSnapshot,
        *,
        package: AggregateAnalysisPackage | None = None,
    ) -> PaperExecutionResult:
        self._assert_runtime(snapshot)
        moment = snapshot.decision_timestamp.astimezone(IST)
        outputs = _agent_rows(decision, package)
        self._journal(
            kind="CANDIDATE",
            timestamp=moment,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={
                "status": decision.status.value,
                "reason_codes": list(decision.reason_codes),
                "risk_guard_result": decision.risk_guard_result,
                "risk_guard_reason": decision.risk_guard_reason,
                "paper_trade_candidate": decision.paper_trade_candidate,
                "executed_flag_on_decision": decision.executed,
            },
        )
        if decision.decision_id in self._seen:
            self._journal(
                kind="REJECTION",
                timestamp=moment,
                decision=decision,
                snapshot=snapshot,
                agent_outputs=outputs,
                payload={"reason": "DUPLICATE_DECISION"},
            )
            existing = self._orders.get(self._by_decision.get(decision.decision_id, ""))
            return self._result("DUPLICATE_DECISION", decision, existing, accepted=False)
        self._seen.add(decision.decision_id)
        if not decision.paper_trade_candidate or decision.risk_guard_result != "APPROVED":
            reason = "NOT_APPROVED:" + ",".join(decision.reason_codes)
            self._journal(
                kind="REJECTION",
                timestamp=moment,
                decision=decision,
                snapshot=snapshot,
                agent_outputs=outputs,
                payload={"reason": reason},
            )
            return self._result(reason, decision, None, accepted=False)
        if decision.snapshot_id != snapshot.snapshot_id or decision.snapshot_version != snapshot.version:
            return self._reject(decision, snapshot, outputs, "SNAPSHOT_MISMATCH", moment)
        if self._enforce_timeout(moment):
            return self._reject(decision, snapshot, outputs, "SESSION_TIMEOUT", moment)
        self._roll_trading_day(moment)
        if self.positions.halted or self.positions.unresolved_close or self._daily_loss_halt:
            return self._reject(decision, snapshot, outputs, "NEW_ENTRIES_BLOCKED", moment)
        floor = -abs(self.config.risk.max_daily_loss)
        day_realized = self.positions.trading_day_realized_pnl(moment)
        day_total = self.positions.trading_day_total_pnl(moment)
        unrealized_only = day_total < floor - 1e-9 and day_realized >= floor - 1e-9
        if unrealized_only:
            self._daily_loss_halt = True
            return self._reject(decision, snapshot, outputs, "DAILY_LOSS_LIMIT", moment)
        quality = gate_snapshot_quality(snapshot)
        if quality is not DataQualityStatus.OK:
            return self._reject(decision, snapshot, outputs, f"DATA_{quality.value}", moment)
        mixed = reject_mixed_market_data(snapshot)
        if mixed:
            return self._reject(decision, snapshot, outputs, mixed, moment)
        unhealthy = reject_unhealthy_market_data(
            data_quality=snapshot.data_quality,
            diagnostics=dict(snapshot.diagnostics or {}),
            freshness_ok=bool((snapshot.diagnostics or {}).get("freshness_ok", True)),
        )
        if unhealthy:
            return self._reject(decision, snapshot, outputs, unhealthy, moment)
        if not self.calendar.allows_new_entries(moment):
            return self._reject(decision, snapshot, outputs, "SESSION_CLOSED", moment)
        if self._occupied() >= self._cap():
            return self._reject(decision, snapshot, outputs, "MAX_POSITIONS", moment)

        instrument = decision.candidate_instrument or ""
        underlying = _text_metric(decision, "underlying")
        quote = match_contract(snapshot, instrument, underlying)
        if quote is None:
            return self._reject(decision, snapshot, outputs, "INSTRUMENT_UNRESOLVED", moment)
        if quote.quality is not DataQualityStatus.OK:
            return self._reject(decision, snapshot, outputs, "DATA_STALE", moment)
        if not quote_known_at(quote, moment):
            return self._reject(decision, snapshot, outputs, "FUTURE_PRICE", moment)
        if decision.direction == "BULLISH" and quote.option_type != "CE":
            return self._reject(decision, snapshot, outputs, "DIRECTION_INSTRUMENT_CONFLICT", moment)
        if decision.direction == "BEARISH" and quote.option_type != "PE":
            return self._reject(decision, snapshot, outputs, "DIRECTION_INSTRUMENT_CONFLICT", moment)

        requested = _float_metric(decision, "limit_price")
        stop = _float_metric(decision, "stop_loss")
        sizing = _resolve_sizing(decision, quote)
        if isinstance(sizing, str):
            return self._reject(decision, snapshot, outputs, sizing, moment)
        lot_size, lots, quantity = sizing
        if requested is None or requested <= 0 or stop is None or stop <= 0:
            return self._reject(decision, snapshot, outputs, "INCOMPLETE_CANDIDATE", moment)
        ticker = contract_id(quote)
        if self.positions.has_open(ticker):
            return self._reject(decision, snapshot, outputs, "DUPLICATE_OPEN_POSITION", moment)

        source = self.policy.entry_source
        simulated = simulate_fill(
            quote,
            source=source,
            side="BUY",
            slippage_bps=self.policy.slippage_bps,
            as_of=moment,
            model_version=self.policy.version,
        )
        if isinstance(simulated, str):
            return self._reject(
                decision,
                snapshot,
                outputs,
                simulated,
                moment,
                quote=quote,
                requested=requested,
                quantity=quantity,
                lot_size=lot_size,
                lots=lots,
            )
        if stop >= simulated.price:
            return self._reject(
                decision,
                snapshot,
                outputs,
                "STOP_THROUGH_MARKET",
                moment,
                quote=quote,
                requested=requested,
                quantity=quantity,
                lot_size=lot_size,
                lots=lots,
                simulated=simulated,
            )

        order = self._new_order(
            decision,
            snapshot,
            quote=quote,
            ticker=ticker,
            quantity=quantity,
            lot_size=lot_size,
            lots=lots,
            requested=requested,
            simulated=simulated,
            moment=moment,
        )
        self._save(order)
        order = self._transition(
            order,
            status=PaperLifecycle.CREATED.value,
            journal_kind="ORDER",
            decision=decision,
            snapshot=snapshot,
            outputs=outputs,
        )
        order = self._transition(
            order,
            status=PaperLifecycle.PENDING.value,
            journal_kind="ORDER",
            decision=decision,
            snapshot=snapshot,
            outputs=outputs,
        )
        self._ensure_universe(ticker)
        moment_clock = FrozenClock(moment)
        self.guard.clock = moment_clock
        self.ledger.clock = moment_clock
        proposal = self._entry_proposal(order, decision, simulated, stop, moment)
        brief = self._brief(proposal.symbol, moment, simulated.price, decision.direction)
        self.last_risk_daily_pnl = self._authoritative_net(moment)
        try:
            verdict = self.guard.evaluate(
                proposal,
                brief,
                cash=self.ledger.book.cash,
                gross_notional=self.ledger.book.gross_notional,
                daily_pnl=self.last_risk_daily_pnl,
                symbol_notional=self.ledger.book.symbol_notional(ticker),
                open_positions=self._occupied(),
            )
        except (GrowLiveTradingDisabled, GrowSafetyError) as exc:
            return self._reject_order(order, decision, snapshot, outputs, str(exc))
        if not verdict.approved or verdict.stamp is None:
            return self._reject_order(order, decision, snapshot, outputs, f"RISK_GUARD:{verdict.reason}")
        try:
            fill = self.ledger.submit(proposal, verdict.stamp)
        except GrowSafetyError as exc:
            return self._reject_order(order, decision, snapshot, outputs, f"LEDGER:{exc}")
        if fill.filled_at.astimezone(IST) < simulated.quote_timestamp:
            raise GrowSafetyError("entry fill timestamp precedes the quote used for the fill")
        take_profit = round(fill.price * (1.0 + self.config.live_data.take_profit_pct), 2)
        position = self.positions.register_open(
            session_id=self.session_id,
            candidate_id=decision.decision_id,
            contract_id=ticker,
            underlying=quote.underlying,
            expiry=quote.expiry,
            strike=quote.strike,
            option_type=quote.option_type,
            provider_id=snapshot.provider,
            lot_size=lot_size,
            lots=lots,
            quantity=quantity,
            entry_price=fill.price,
            opened_at=fill.filled_at,
            stop_loss_price=stop,
            take_profit_price=take_profit,
            fill_id=fill.fill_id,
            snapshot_id=snapshot.snapshot_id,
        )
        order = replace(
            order,
            status=PaperLifecycle.FILLED.value,
            fill_status=FillStatus.FILLED.value,
            execution_price=fill.price,
            slippage=simulated.slippage,
            price_source=simulated.source,
            reference_price=simulated.reference,
            position_id=position.position_id,
        )
        self._save(order)
        filled_record = self._journal(
            kind="FILL",
            timestamp=fill.filled_at,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload=_fill_payload(order, simulated, fill, lot_size=lot_size, lots=lots, quantity=quantity),
        )
        order = replace(order, status=PaperLifecycle.OPEN.value, journal_record_id=filled_record.record_id)
        self._save(order)
        self._journal(
            kind="POSITION",
            timestamp=fill.filled_at,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={
                "position_id": position.position_id,
                "state": position.state.value,
                "underlying": quote.underlying,
                "expiry": quote.expiry.isoformat(),
                "strike": quote.strike,
                "option_type": quote.option_type,
                "provider_contract_id": quote.provider_contract_id,
                "lot_size": lot_size,
                "lots": lots,
                "quantity": quantity,
                "notional": expected_notional(quantity, fill.price),
                "order": order.to_dict(),
            },
        )
        self._journal(
            kind="OUTCOME",
            timestamp=fill.filled_at,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={"accepted": True, "position_id": position.position_id, "paper_order_id": order.paper_order_id},
        )
        self._journal_pnl(snapshot, decision)
        return self._result("FILLED", decision, order, accepted=True, position_id=position.position_id)

    def on_snapshot(self, snapshot: AgentMarketSnapshot) -> tuple[str, ...]:
        """Mark, exit, expire, and account. Stale data does not open or reprice.

        Session timeout stops new entries and marks unresolved closes, but does
        not permanently freeze recovery: when valid fresh quotes arrive, pending
        closes are retried without fabricating prices.
        """
        self._assert_runtime(snapshot)
        moment = snapshot.decision_timestamp.astimezone(IST)
        reasons: list[str] = []
        timed_out = self._enforce_timeout(moment)
        if timed_out:
            reasons.append("SESSION_TIMEOUT")
        if gate_snapshot_quality(snapshot) is not DataQualityStatus.OK:
            self._journal(
                kind="REJECTION",
                timestamp=moment,
                decision=None,
                snapshot=snapshot,
                agent_outputs=(),
                payload={"reason": "DATA_STALE", "scope": "MARK", "timeout": timed_out},
            )
            reasons.append("DATA_STALE")
            self._journal_pnl(snapshot, None)
            return tuple(reasons)
        for position in self.positions.open_positions():
            if position.expiry < snapshot.session_date:
                reasons.append(self._close_position(position, snapshot, exit_reason="EXPIRED"))
        live = live_snapshot_from_agent(snapshot, sequence=self._mark_sequence)
        self._mark_sequence += 1
        self.positions.mark(live)
        if timed_out or (self._timeout_recorded and self.positions.unresolved_close):
            recovery = self._retry_timeout_closes(snapshot)
            reasons.extend(recovery)
        else:
            for exit_decision in self.positions.exits(live):
                position = self.positions.get(exit_decision.position_id)
                if position is None or position.state is not PositionState.OPEN:
                    continue
                reasons.append(
                    self._close_position(
                        position,
                        snapshot,
                        exit_reason=exit_decision.reason,
                    )
                )
        self._journal_pnl(snapshot, None)
        return tuple(reason for reason in reasons if reason)

    def _retry_timeout_closes(self, snapshot: AgentMarketSnapshot) -> list[str]:
        """After timeout, close remaining open inventory once fresh quotes exist."""
        outcomes: list[str] = []
        open_rows = list(self.positions.open_positions())
        if not open_rows:
            if self.positions.unresolved_close:
                self.positions.unresolved_close = False
                self._journal(
                    kind="OUTCOME",
                    timestamp=snapshot.decision_timestamp,
                    decision=None,
                    snapshot=snapshot,
                    agent_outputs=(),
                    payload={
                        "reason": "TIMEOUT_RECOVERY_CLEAR",
                        "unresolved_close": False,
                        "halted": self.positions.halted,
                    },
                )
            return outcomes
        for position in open_rows:
            if position.state is not PositionState.OPEN:
                continue
            result = self._close_position(position, snapshot, exit_reason=ExitReason.SESSION_TIMEOUT)
            outcomes.append(result)
        still_open = self.positions.open_positions()
        if not still_open and any(item == ExitReason.SESSION_TIMEOUT for item in outcomes):
            self.positions.unresolved_close = False
            # Session halt remains: new entries stay blocked after timeout.
            self.positions.halted = True
            self._journal(
                kind="OUTCOME",
                timestamp=snapshot.decision_timestamp,
                decision=None,
                snapshot=snapshot,
                agent_outputs=(),
                payload={
                    "reason": "TIMEOUT_RECOVERY_CLOSED",
                    "unresolved_close": False,
                    "halted": True,
                    "closes": [item for item in outcomes if item == ExitReason.SESSION_TIMEOUT],
                },
            )
        return outcomes

    def export_state(self) -> dict[str, Any]:
        """Replayable checkpoint. Journal rows are a copy, not a live alias."""
        book = self.ledger.book
        return {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "broker_order_calls": 0,
            "seen": sorted(self._seen),
            "daily_loss_halt": self._daily_loss_halt,
            "pnl_day": None if self._pnl_day is None else self._pnl_day.isoformat(),
            "timeout_recorded": self._timeout_recorded,
            "mark_sequence": self._mark_sequence,
            "last_risk_daily_pnl": self.last_risk_daily_pnl,
            "journal": self.journal.to_list(),
            "orders": [order.to_dict() for order in self._orders.values()],
            "positions": [row.to_dict() for row in self.positions.all()],
            "registry": {
                "opens": self.positions.opens,
                "closes": self.positions.closes,
                "stop_loss_exits": self.positions.stop_loss_exits,
                "take_profit_exits": self.positions.take_profit_exits,
                "session_close_exits": self.positions.session_close_exits,
                "no_trade_count": self.positions.no_trade_count,
                "valuation_gaps": self.positions.valuation_gaps,
                "halted": self.positions.halted,
                "unresolved_close": self.positions.unresolved_close,
            },
            "ledger": {
                "cash": book.cash,
                "realized_pnl": book.realized_pnl,
                "positions": [
                    {
                        "ticker": pos.symbol.ticker,
                        "quantity": pos.quantity,
                        "average_price": pos.average_price,
                    }
                    for pos in book.positions.values()
                ],
                "fills": [fill.to_dict() for fill in book.fills],
            },
            "paper_mode": True,
            "live_trading": False,
        }

    @classmethod
    def restore(
        cls,
        config: GrowConfig,
        state: Mapping[str, Any],
        *,
        clock: Clock | None = None,
        risk_secret: str | None = None,
    ) -> PaperExecutionEngine:
        engine = cls(config, clock=clock, risk_secret=risk_secret)
        engine.session_id = str(state["session_id"])
        engine.started_at = datetime.fromisoformat(str(state["started_at"]))
        engine.journal.load(list(state["journal"]))
        engine._seen = set(state["seen"])
        engine._daily_loss_halt = bool(state["daily_loss_halt"])
        raw_day = state.get("pnl_day")
        engine._pnl_day = None if raw_day in (None, "") else date.fromisoformat(str(raw_day))
        engine._timeout_recorded = bool(state["timeout_recorded"])
        engine._mark_sequence = int(state["mark_sequence"])
        engine.last_risk_daily_pnl = state.get("last_risk_daily_pnl")
        engine.broker_order_calls = 0
        for row in state["positions"]:
            engine.positions.adopt(PaperPosition.from_dict(dict(row)))
        registry = state["registry"]
        engine.positions.opens = int(registry["opens"])
        engine.positions.closes = int(registry["closes"])
        engine.positions.stop_loss_exits = int(registry["stop_loss_exits"])
        engine.positions.take_profit_exits = int(registry["take_profit_exits"])
        engine.positions.session_close_exits = int(registry["session_close_exits"])
        engine.positions.no_trade_count = int(registry["no_trade_count"])
        engine.positions.valuation_gaps = int(registry["valuation_gaps"])
        engine.positions.halted = bool(registry["halted"])
        engine.positions.unresolved_close = bool(registry["unresolved_close"])
        ledger = state["ledger"]
        engine.ledger.book.cash = float(ledger["cash"])
        engine.ledger.book.realized_pnl = float(ledger["realized_pnl"])
        engine.ledger.book.positions = {
            row["ticker"]: Position(
                symbol=Symbol(row["ticker"]),
                quantity=int(row["quantity"]),
                average_price=float(row["average_price"]),
            )
            for row in ledger["positions"]
        }
        engine.ledger.book.fills = [_fill_from_dict(row) for row in ledger["fills"]]
        for row in state["orders"]:
            order = PaperOrder.from_dict(row)
            engine._orders[order.paper_order_id] = order
            engine._by_decision[order.decision_id] = order.paper_order_id
        return engine

    def orders(self) -> tuple[PaperOrder, ...]:
        return tuple(self._orders.values())

    def _close_position(self, position: PaperPosition, snapshot: AgentMarketSnapshot, *, exit_reason: str) -> str:
        moment = snapshot.decision_timestamp.astimezone(IST)
        if moment <= position.opened_at.astimezone(IST):
            return self._abort_exit(position, snapshot, exit_reason, "EXIT_BEFORE_ENTRY")
        quote = _quote_for(snapshot, position.contract_id)
        if quote is None:
            return self._abort_exit(position, snapshot, exit_reason, "NO_FILL_QUOTE")
        if quote.quality is not DataQualityStatus.OK:
            return self._abort_exit(position, snapshot, exit_reason, "DATA_STALE")
        source = deterministic_exit_source(quote) if self.policy.deterministic else self.policy.exit_source
        if source is None:
            return self._abort_exit(position, snapshot, exit_reason, "NO_FILL_QUOTE")
        simulated = simulate_fill(
            quote,
            source=source,
            side="SELL",
            slippage_bps=self.policy.slippage_bps,
            as_of=moment,
            model_version=self.policy.version,
        )
        if isinstance(simulated, str):
            return self._abort_exit(position, snapshot, exit_reason, simulated)
        pending = self.positions.begin_exit(position.position_id)
        if pending is None:
            return "DUPLICATE_CLOSE"
        order = self._order_for_position(position.position_id)
        decision = _decision_stub(position, order)
        outputs = () if order is None else ()
        if order is not None and order.status not in _TERMINAL:
            order = replace(order, status=PaperLifecycle.EXIT_REQUESTED.value)
            self._save(order)
        self._journal(
            kind="EXIT",
            timestamp=moment,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={
                "position_id": position.position_id,
                "exit_reason": exit_reason,
                "state": PaperLifecycle.EXIT_REQUESTED.value,
                "price_source": simulated.source,
            },
        )
        self._ensure_universe(position.contract_id)
        moment_clock = FrozenClock(moment)
        self.guard.clock = moment_clock
        self.ledger.clock = moment_clock
        intent = Intent.SQUARE_OFF if exit_reason in {ExitReason.SESSION_CLOSE, ExitReason.SESSION_TIMEOUT, "EXPIRED"} else Intent.CLOSE
        proposal = TradeProposal(
            proposal_id=f"px-{position.position_id}-{exit_reason}",
            symbol=Symbol(position.contract_id),
            side=Side.SELL,
            intent=intent,
            quantity=position.quantity,
            limit_price=simulated.price,
            stop_loss=None,
            take_profit=None,
            thesis=f"Paper {exit_reason}",
            confidence=1.0,
            venue=Venue.PAPER,
            created_at=moment,
            notional=expected_notional(position.quantity, simulated.price),
            extras={"position_id": position.position_id, "exit_reason": exit_reason, "broker_order_path": False},
        )
        brief = self._brief(proposal.symbol, moment, simulated.price, None)
        try:
            verdict = self.guard.evaluate_exit(
                proposal,
                brief,
                cash=self.ledger.book.cash,
                gross_notional=self.ledger.book.gross_notional,
                daily_pnl=self._authoritative_net(moment),
                symbol_notional=self.ledger.book.symbol_notional(position.contract_id),
            )
        except (GrowLiveTradingDisabled, GrowSafetyError) as exc:
            return self._abort_exit(position, snapshot, exit_reason, str(exc), started=True)
        self.last_risk_daily_pnl = self._authoritative_net(moment)
        if not verdict.approved or verdict.stamp is None:
            return self._abort_exit(position, snapshot, exit_reason, f"RISK_GUARD:{verdict.reason}", started=True)
        try:
            fill = self.ledger.submit(proposal, verdict.stamp)
        except GrowSafetyError as exc:
            return self._abort_exit(position, snapshot, exit_reason, f"LEDGER:{exc}", started=True)
        if fill.filled_at.astimezone(IST) <= position.opened_at.astimezone(IST):
            raise GrowSafetyError("exit timestamp must follow entry")
        if simulated.quote_timestamp > fill.filled_at.astimezone(IST):
            raise GrowSafetyError("exit used a quote from after the exit timestamp")
        costs = self.costs.round_trip(
            entry=position.entry_price,
            exit=fill.price,
            quantity=position.lots,
            lot_size=position.lot_size,
        )
        closed = self.positions.complete_close(
            position.position_id,
            exit_reason=exit_reason,
            exit_price=fill.price,
            closed_at=fill.filled_at,
            costs=costs,
            close_fill_id=fill.fill_id,
            snapshot_id=snapshot.snapshot_id,
        )
        if abs(closed.realized_pnl - (closed.realized_gross - closed.total_costs)) > 1e-6:
            raise GrowSafetyError("PNL_RECONCILIATION_FAILED")
        summary = self.positions.summary()
        if abs(summary.net_realized_pnl - (summary.gross_realized_pnl - summary.total_costs)) > 1e-6:
            raise GrowSafetyError("PNL_RECONCILIATION_FAILED")
        terminal = PaperLifecycle.EXPIRED.value if exit_reason == "EXPIRED" else PaperLifecycle.CLOSED.value
        if order is not None:
            order = replace(
                order,
                status=terminal,
                exit_reason=exit_reason,
                price_source=simulated.source,
            )
            self._save(order)
        self._journal(
            kind="FILL",
            timestamp=fill.filled_at,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={
                "side": "SELL",
                "execution_price": fill.price,
                "reference_price": simulated.reference,
                "price_source": simulated.source,
                "slippage": simulated.slippage,
                "slippage_bps": simulated.slippage_bps,
                "slippage_model_version": simulated.model_version,
                "fill_model_version": simulated.model_version,
                "fee_model_version": self.costs.version,
                "lot_size": position.lot_size,
                "lots": position.lots,
                "quantity": position.quantity,
                "notional": expected_notional(position.quantity, fill.price),
                "quote_timestamp": simulated.quote_timestamp.isoformat(),
                "fill_timestamp": fill.filled_at.isoformat(),
            },
        )
        self._journal(
            kind="POSITION",
            timestamp=fill.filled_at,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={"position_id": closed.position_id, "state": closed.state.value, "exit_reason": exit_reason},
        )
        self._journal(
            kind="PNL",
            timestamp=fill.filled_at,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={
                "position_id": closed.position_id,
                "gross_pnl": closed.realized_gross,
                "costs": closed.total_costs,
                "net_pnl": closed.realized_pnl,
                "authoritative_net_pnl": summary.net_realized_pnl,
                "identity": "net_realized_pnl == gross_realized_pnl - total_costs",
            },
        )
        self._journal(
            kind="OUTCOME",
            timestamp=fill.filled_at,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={"position_id": closed.position_id, "status": terminal, "exit_reason": exit_reason},
        )
        return exit_reason

    def _abort_exit(
        self,
        position: PaperPosition,
        snapshot: AgentMarketSnapshot,
        exit_reason: str,
        reason: str,
        *,
        started: bool = False,
    ) -> str:
        if started or self.positions.get(position.position_id) and self.positions.get(position.position_id).state is PositionState.EXIT_PENDING:  # type: ignore[union-attr]
            self.positions.abort_exit(position.position_id, reason)
        else:
            position.diagnostics.append(reason)
        if exit_reason in {ExitReason.SESSION_CLOSE, ExitReason.SESSION_TIMEOUT, "EXPIRED"}:
            self.positions.unresolved_close = True
            self.positions.halted = True
        order = self._order_for_position(position.position_id)
        if order is not None and order.status == PaperLifecycle.EXIT_REQUESTED.value:
            self._save(replace(order, status=PaperLifecycle.OPEN.value, rejection_reason=reason))
        self._journal(
            kind="REJECTION",
            timestamp=snapshot.decision_timestamp,
            decision=_decision_stub(position, order),
            snapshot=snapshot,
            agent_outputs=(),
            payload={"reason": reason, "exit_reason": exit_reason, "position_id": position.position_id},
        )
        return reason

    def _reject(
        self,
        decision: IntegratedDecision,
        snapshot: AgentMarketSnapshot,
        outputs: tuple[Mapping[str, Any], ...],
        reason: str,
        moment: datetime,
        *,
        quote=None,
        requested: float | None = None,
        quantity: int | None = None,
        lot_size: int | None = None,
        lots: int | None = None,
        simulated: FillSimulation | None = None,
    ) -> PaperExecutionResult:
        order = self._new_order(
            decision,
            snapshot,
            quote=quote,
            ticker=contract_id(quote) if quote is not None else (decision.candidate_instrument or decision.decision_id),
            quantity=quantity or 0,
            lot_size=lot_size,
            lots=lots,
            requested=requested,
            simulated=simulated,
            moment=moment,
        )
        order = replace(
            order,
            status=PaperLifecycle.REJECTED.value,
            fill_status=FillStatus.REJECTED.value,
            rejection_reason=reason,
            quantity=quantity or 0,
        )
        self._save(order)
        record = self._journal(
            kind="REJECTION",
            timestamp=moment,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={"reason": reason, "paper_order_id": order.paper_order_id, "order": order.to_dict()},
        )
        self._save(replace(order, journal_record_id=record.record_id))
        self._journal(
            kind="OUTCOME",
            timestamp=moment,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={"accepted": False, "reason": reason, "paper_order_id": order.paper_order_id},
        )
        return self._result(reason, decision, order, accepted=False)

    def _reject_order(
        self,
        order: PaperOrder,
        decision: IntegratedDecision,
        snapshot: AgentMarketSnapshot,
        outputs: tuple[Mapping[str, Any], ...],
        reason: str,
    ) -> PaperExecutionResult:
        order = replace(
            order,
            status=PaperLifecycle.REJECTED.value,
            fill_status=FillStatus.REJECTED.value,
            rejection_reason=reason,
        )
        self._save(order)
        record = self._journal(
            kind="REJECTION",
            timestamp=order.timestamp,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={"reason": reason, "paper_order_id": order.paper_order_id},
        )
        self._save(replace(order, journal_record_id=record.record_id))
        return self._result(reason, decision, order, accepted=False)

    def _new_order(
        self,
        decision: IntegratedDecision,
        snapshot: AgentMarketSnapshot,
        *,
        quote,
        ticker: str,
        quantity: int,
        requested: float | None,
        simulated: FillSimulation | None,
        moment: datetime,
        lot_size: int | None = None,
        lots: int | None = None,
    ) -> PaperOrder:
        paper_order_id = "po-" + digest_payload({"decision_id": decision.decision_id})
        fees = _fee_assumptions(self.costs)
        return PaperOrder(
            paper_order_id=paper_order_id,
            decision_id=decision.decision_id,
            analysis_cycle_id=decision.analysis_cycle_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            instrument=decision.candidate_instrument or ticker,
            token=None if quote is None else quote.provider_contract_id,
            symbol=ticker,
            underlying=None if quote is None else quote.underlying,
            expiry=None if quote is None else quote.expiry,
            strike=None if quote is None else quote.strike,
            option_type=None if quote is None else quote.option_type,
            lot_size=lot_size,
            lots=lots,
            quantity=quantity,
            side="BUY",
            direction=decision.direction,
            order_type="LIMIT",
            requested_price=requested,
            execution_price=None if simulated is None else simulated.price,
            reference_price=None if simulated is None else simulated.reference,
            status=PaperLifecycle.CREATED.value,
            fill_status=FillStatus.UNFILLED.value,
            rejection_reason=None,
            fee_model_version=self.costs.version,
            slippage_model_version=self.policy.version,
            slippage_bps=self.policy.slippage_bps,
            slippage=0.0 if simulated is None else simulated.slippage,
            price_source=None if simulated is None else simulated.source,
            fee_assumptions=fees,
            timestamp=moment,
            source_snapshot_id=snapshot.snapshot_id,
        )

    def _transition(
        self,
        order: PaperOrder,
        *,
        status: str,
        journal_kind: str,
        decision: IntegratedDecision,
        snapshot: AgentMarketSnapshot,
        outputs: tuple[Mapping[str, Any], ...],
    ) -> PaperOrder:
        order = replace(order, status=status)
        self._save(order)
        record = self._journal(
            kind=journal_kind,
            timestamp=order.timestamp,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=outputs,
            payload={"paper_order_id": order.paper_order_id, "status": status, "order": order.to_dict()},
        )
        order = replace(order, journal_record_id=record.record_id)
        self._save(order)
        return order

    def _entry_proposal(
        self,
        order: PaperOrder,
        decision: IntegratedDecision,
        simulated: FillSimulation,
        stop: float,
        moment: datetime,
    ) -> TradeProposal:
        take_profit = round(simulated.price * (1.0 + self.config.live_data.take_profit_pct), 2)
        return TradeProposal(
            proposal_id=f"po-prop-{order.paper_order_id}",
            symbol=Symbol(order.symbol),
            side=Side.BUY,
            intent=Intent.OPEN,
            quantity=order.quantity,
            limit_price=simulated.price,
            stop_loss=stop,
            take_profit=take_profit,
            thesis=f"4C paper candidate {decision.candidate_strategy or ''} {order.instrument}".strip(),
            confidence=0.0 if _float_metric(decision, "confidence") is None else float(_float_metric(decision, "confidence") or 0.0),
            venue=Venue.PAPER,
            created_at=moment,
            notional=expected_notional(order.quantity, simulated.price),
            extras={
                "decision_id": decision.decision_id,
                "paper_order_id": order.paper_order_id,
                "analysis_cycle_id": decision.analysis_cycle_id,
                "broker_order_path": False,
            },
        )

    def _brief(self, symbol: Symbol, moment: datetime, price: float, direction: str | None) -> MarketBrief:
        regime = Regime.TRENDING_UP if direction == "BULLISH" else Regime.TRENDING_DOWN if direction == "BEARISH" else Regime.UNKNOWN
        return MarketBrief(
            symbol=symbol,
            as_of=moment,
            session=self.calendar.state(moment),
            last_price=price,
            currency=self.config.market.currency,
            regime=regime,
            source="grow.paper.engine",
            extras={"broker_order_path": False},
        )

    def _journal(
        self,
        *,
        kind: str,
        timestamp: datetime,
        decision: IntegratedDecision | None,
        snapshot: AgentMarketSnapshot | None,
        agent_outputs: tuple[Mapping[str, Any], ...],
        payload: Mapping[str, Any],
    ):
        return self.journal.append(
            kind=kind,
            timestamp=timestamp,
            decision_id=None if decision is None else decision.decision_id,
            cycle_id=None if decision is None else decision.analysis_cycle_id,
            snapshot_id=None if snapshot is None else snapshot.snapshot_id,
            snapshot_version=None if snapshot is None else snapshot.version,
            agent_outputs=agent_outputs,
            payload=payload,
        )

    def _journal_pnl(self, snapshot: AgentMarketSnapshot, decision: IntegratedDecision | None) -> None:
        summary = self.positions.summary()
        self._journal(
            kind="PNL",
            timestamp=snapshot.decision_timestamp,
            decision=decision,
            snapshot=snapshot,
            agent_outputs=(),
            payload={
                "summary": summary.to_dict(),
                "authoritative_net_pnl": summary.net_realized_pnl,
                "unrealized_pnl": summary.unrealized_pnl,
                "open_exposure": summary.open_exposure,
                "trade_count": summary.closes,
            },
        )

    def _save(self, order: PaperOrder) -> None:
        self._orders[order.paper_order_id] = order
        self._by_decision[order.decision_id] = order.paper_order_id

    def _order_for_position(self, position_id: str) -> PaperOrder | None:
        for order in self._orders.values():
            if order.position_id == position_id:
                return order
        return None

    def _result(
        self,
        reason: str,
        decision: IntegratedDecision,
        order: PaperOrder | None,
        *,
        accepted: bool,
        position_id: str | None = None,
    ) -> PaperExecutionResult:
        return PaperExecutionResult(
            accepted=accepted,
            reason=reason,
            decision_id=decision.decision_id,
            paper_order_id=None if order is None else order.paper_order_id,
            position_id=position_id if position_id is not None else (None if order is None else order.position_id),
            execution_price=None if order is None else order.execution_price,
            price_source=None if order is None else order.price_source,
            broker_order_calls=0,
        )

    def _occupied(self) -> int:
        return sum(1 for row in self.positions.all() if row.state in {PositionState.OPEN, PositionState.EXIT_PENDING})

    def _cap(self) -> int:
        if self.config.risk.max_open_positions is not None:
            return self.config.risk.max_open_positions
        return self.config.backtest.max_open_positions

    def _authoritative_net(self, moment: datetime | None = None) -> float:
        """Trading-day realized P&L fed to Risk Guard (excludes prior days)."""
        when = moment if moment is not None else self.clock.now()
        return self.positions.trading_day_realized_pnl(when)

    def _loss_breached(self, moment: datetime | None = None) -> bool:
        when = moment if moment is not None else self.clock.now()
        self._roll_trading_day(when)
        floor = -abs(self.config.risk.max_daily_loss)
        day_total = self.positions.trading_day_total_pnl(when)
        day_realized = self.positions.trading_day_realized_pnl(when)
        return day_total < floor - 1e-9 or day_realized < floor - 1e-9

    def _roll_trading_day(self, moment: datetime) -> None:
        """Advance the loss budget to the IST calendar day; prior-day halt does not carry over."""
        day = moment.astimezone(IST).date()
        if self._pnl_day is None:
            self._pnl_day = day
            return
        if day != self._pnl_day:
            self._pnl_day = day
            self._daily_loss_halt = False

    def _enforce_timeout(self, moment: datetime) -> bool:
        elapsed = (self.clock.now() - self.started_at).total_seconds()
        if elapsed < self.config.live_data.session_timeout_seconds:
            return False
        if self._timeout_recorded:
            # Still timed out — block new entries — but do not prevent close recovery.
            return True
        self._timeout_recorded = True
        if self.positions.open_positions():
            self.positions.halt_for_timeout()
            reason = "SESSION_TIMEOUT_WITH_OPEN_POSITION"
        else:
            self.positions.halted = True
            reason = "SESSION_TIMEOUT"
        self._journal(
            kind="REJECTION",
            timestamp=moment,
            decision=None,
            snapshot=None,
            agent_outputs=(),
            payload={"reason": reason, "open_positions": len(self.positions.open_positions())},
        )
        return True

    def _ensure_universe(self, ticker: str) -> None:
        current = self.guard.config.market.universe
        if ticker in current:
            return
        cfg = replace(self.guard.config, market=replace(self.guard.config.market, universe=(*current, ticker)))
        self.guard = RiskGuard(cfg, clock=self.clock, secret=self._risk_secret)
        self.ledger.guard = self.guard
        self.ledger.config = cfg
        self._assert_limits()

    def _assert_limits(self) -> None:
        if _limit_tuple(self.config) != self._limits or _limit_tuple(self.guard.config) != self._limits:
            raise GrowSafetyError("Risk Guard limits changed at runtime")

    def _assert_runtime(self, snapshot: AgentMarketSnapshot | None) -> None:
        assert_paper_runtime(self.config.execution.mode, self.config.execution.live_trading_enabled, self.config.paper.venue_id)
        self._assert_limits()
        if snapshot is not None and (snapshot.live_trading or not snapshot.paper_mode):
            raise GrowLiveTradingDisabled("snapshot must remain paper-only")
        self.broker_order_calls = 0


def _limit_tuple(config: GrowConfig) -> tuple:
    risk = config.risk
    return (
        risk.max_daily_loss,
        risk.max_position_notional,
        risk.max_gross_notional,
        risk.max_open_positions,
        risk.max_per_trade_risk,
        risk.ruleset,
        risk.allow_short,
        risk.max_symbol_concentration,
        risk.require_stop_loss,
        config.execution.mode,
        config.execution.live_trading_enabled,
    )


def _fee_assumptions(costs: CostModel) -> dict[str, Any]:
    return {
        "model": costs.version,
        "brokerage_per_order": costs.brokerage_per_order,
        "stt_sell_pct": costs.stt_sell_pct,
        "exchange_pct": costs.exchange_pct,
        "gst_pct": costs.gst_pct,
        "stress": costs.stress,
    }


def _resolve_sizing(decision: IntegratedDecision, quote) -> tuple[int, int, int] | str:
    """lot_size from contract metadata only; lots must be explicit on the decision.

    Phase 5 fail-closed rules:
    - Never fall back to decision.lot_size when quote.lot_size is missing.
    - Missing quote lot_size → MISSING_LOT_SIZE.
    - Missing decision lots → MISSING_LOTS (quantity alone is not enough).
    - quantity = lot_size × lots; optional quantity metric must match.
    """
    lot_size = quote.lot_size
    metric_lot = _int_metric(decision, "lot_size")
    if lot_size is None or lot_size < 1:
        return "MISSING_LOT_SIZE"
    if metric_lot is not None and metric_lot != lot_size:
        return "LOT_SIZE_MISMATCH"
    lots = _int_metric(decision, "lots")
    if lots is None:
        return "MISSING_LOTS"
    if lots < 1:
        return "MISSING_LOTS"
    quantity = lots * lot_size
    quantity_metric = _int_metric(decision, "quantity")
    if quantity_metric is not None and quantity_metric != quantity:
        return "LOT_QUANTITY_MISMATCH"
    if quantity < 1:
        return "LOT_QUANTITY_MISMATCH"
    return lot_size, lots, quantity


def _fill_payload(
    order: PaperOrder,
    simulated: FillSimulation,
    fill: Fill,
    *,
    lot_size: int,
    lots: int,
    quantity: int,
) -> dict[str, Any]:
    return {
        "paper_order_id": order.paper_order_id,
        "requested_price": order.requested_price,
        "execution_price": fill.price,
        "reference_price": simulated.reference,
        "price_source": simulated.source,
        "slippage": simulated.slippage,
        "slippage_bps": simulated.slippage_bps,
        "slippage_model_version": simulated.model_version,
        "fill_model_version": simulated.model_version,
        "fee_model_version": order.fee_model_version,
        "fee_assumptions": dict(order.fee_assumptions),
        "lot_size": lot_size,
        "lots": lots,
        "quantity": quantity,
        "notional": expected_notional(quantity, fill.price),
        "underlying": order.underlying,
        "expiry": None if order.expiry is None else order.expiry.isoformat(),
        "strike": order.strike,
        "option_type": order.option_type,
        "provider_contract_id": order.token,
        "quote_timestamp": simulated.quote_timestamp.isoformat(),
        "fill_timestamp": fill.filled_at.isoformat(),
        "fill_id": fill.fill_id,
    }


def _agent_rows(decision: IntegratedDecision, package: AggregateAnalysisPackage | None) -> tuple[dict[str, Any], ...]:
    rows = [ref.to_dict() for ref in decision.agent_output_refs]
    if package is None:
        return tuple(rows)
    for output in package.agent_outputs:
        if hasattr(output, "to_dict"):
            rows.append(output.to_dict())
    return tuple(rows)


def _metrics(decision: IntegratedDecision) -> dict[str, Any]:
    relied = {ref.agent_name for ref in decision.agent_output_refs if ref.relied_upon}
    merged: dict[str, Any] = {}
    for name, metrics in decision.calculated_evidence.items():
        if not isinstance(metrics, Mapping):
            continue
        if relied and name not in relied:
            continue
        for key, value in metrics.items():
            merged.setdefault(str(key), value)
    if merged:
        return merged
    for metrics in decision.calculated_evidence.values():
        if isinstance(metrics, Mapping):
            return {str(key): value for key, value in metrics.items()}
    return {}


def _text_metric(decision: IntegratedDecision, key: str) -> str | None:
    value = _metrics(decision).get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _float_metric(decision: IntegratedDecision, key: str) -> float | None:
    value = _metrics(decision).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int_metric(decision: IntegratedDecision, key: str) -> int | None:
    value = _metrics(decision).get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _quote_for(snapshot: AgentMarketSnapshot, contract: str):
    for quote in snapshot.option_contracts:
        if contract_id(quote) == contract or quote.provider_contract_id == contract:
            return quote
    return None


def _decision_stub(position: PaperPosition, order: PaperOrder | None) -> IntegratedDecision | None:
    """Journal linkage for exits uses the stored order's decision id without rebuilding 4C."""
    if order is None:
        return None
    return _LinkDecision(order.decision_id, order.analysis_cycle_id)  # type: ignore[return-value]


class _LinkDecision:
    """Minimal decision identity for journal rows that are not a new 4C integration."""

    def __init__(self, decision_id: str, analysis_cycle_id: str) -> None:
        self.decision_id = decision_id
        self.analysis_cycle_id = analysis_cycle_id
        self.executed = False


def _fill_from_dict(payload: Mapping[str, Any]) -> Fill:
    symbol = payload["symbol"]
    return Fill(
        fill_id=str(payload["fill_id"]),
        proposal_id=str(payload["proposal_id"]),
        symbol=Symbol(symbol["ticker"], symbol.get("exchange", "NSE")),
        side=Side(payload["side"]),
        quantity=int(payload["quantity"]),
        price=float(payload["price"]),
        notional=float(payload["notional"]),
        filled_at=datetime.fromisoformat(str(payload["filled_at"])),
        venue_id=str(payload["venue_id"]),
    )
