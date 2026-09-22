"""Live stream → 2A/2B/2C/2D → Risk Guard → PaperLedger. Never a broker."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from grow.backtest.costs import CostModel, SlippageModel, contract_pnl
from grow.backtest.pipeline import pick_signal
from grow.backtest.simulate import ExecutionSimulator
from grow.clock import IST, Clock, FrozenClock
from grow.config import GrowConfig
from grow.data.boundary import research_view
from grow.errors import GrowConfigError, GrowLiveTradingDisabled
from grow.execution.lock import LIVE_TRADING_COMPILED, assert_paper_runtime
from grow.history.universe import default_index_registry
from grow.live_data.models import (
    APPROVED_STREAM_IDS,
    CycleStatus,
    LiveCycleReport,
    LiveHealth,
    LiveSessionRecord,
    LiveSnapshot,
    SessionHealth,
)
from grow.live_data.normalize import normalize_event
from grow.live_data.protocol import CONTROL_KINDS
from grow.live_data.provider import LiveDataProvider, open_provider
from grow.market.session import SessionCalendar
from grow.options.engine import IndexOptionsEngine
from grow.options.models import DecisionStatus
from grow.paper.exits import ExitDecision, ExitReason
from grow.paper.ledger import PaperLedger, expected_notional
from grow.paper.positions import PositionRegistry, PositionState
from grow.research.models import CEOVerdict
from grow.research.orchestrator import ResearchOrchestrator
from grow.research.packet import build_packet
from grow.risk.guard import RiskGuard
from grow.strategies.engine import StrategyEngine
from grow.types import Intent, MarketBrief, Regime, Side, Symbol, TradeProposal, Venue


def _no_trade(
    *,
    session_id: str,
    event_id: str,
    sequence: int,
    as_of: datetime,
    underlying: str,
    reason: str,
    provider_id: str,
    health: SessionHealth,
    snapshot_id: str | None = None,
    candidate_id: str | None = None,
    decision_id: str | None = None,
    extras: dict[str, Any] | None = None,
) -> LiveCycleReport:
    return LiveCycleReport(
        session_id=session_id,
        event_id=event_id,
        sequence=sequence,
        as_of=as_of,
        underlying=underlying,
        status=CycleStatus.NO_TRADE,
        reason=reason,
        provider_id=provider_id,
        snapshot_id=snapshot_id,
        candidate_id=candidate_id,
        decision_id=decision_id,
        option_type=None,
        expiry=None,
        strike=None,
        lots=None,
        lot_size=None,
        fill=None,
        verdict=None,
        health=health,
        extras=extras or {},
    )


class LivePaperLoop:
    def __init__(
        self,
        config: GrowConfig,
        provider: LiveDataProvider,
        *,
        clock: Clock | None = None,
        risk_secret: str | None = None,
    ) -> None:
        if LIVE_TRADING_COMPILED:
            raise GrowLiveTradingDisabled("LIVE_TRADING_COMPILED forbids 3A")
        config.assert_safe()
        if not config.live_data.enabled:
            raise GrowConfigError("live_data.enabled must be true to start the paper loop")
        if config.live_data.live_trading or not config.live_data.paper_mode:
            raise GrowLiveTradingDisabled("3A paper loop requires paper_mode=true live_trading=false")
        if config.execution.live_trading_enabled:
            raise GrowLiveTradingDisabled("live trading remains disabled")
        assert_paper_runtime(config.execution.mode, config.execution.live_trading_enabled, config.paper.venue_id)
        self.config = config
        self.provider = provider
        self.clock = clock or FrozenClock(datetime.now(tz=IST))
        self.calendar = SessionCalendar(config.market, clock=self.clock)
        self._risk_secret = risk_secret
        self._engine_config = replace(config, options=replace(config.options, provider="paper_stream"))
        self.strategies = StrategyEngine(config)
        self.options = IndexOptionsEngine(self._engine_config)
        self.research = ResearchOrchestrator(config)
        self.guard = RiskGuard(config, clock=self.clock, secret=risk_secret)
        self.ledger = PaperLedger(config, self.guard, clock=self.clock)
        self.simulator = ExecutionSimulator(SlippageModel(config.backtest.slippage_bps), quantity=config.live_data.quantity)
        self.costs = CostModel()
        self.positions = PositionRegistry(
            starting_cash=config.paper.starting_cash,
            clock=self.clock,
            calendar=self.calendar,
            session_close_square_off=config.live_data.session_close_square_off,
        )
        self.session = LiveSessionRecord(
            session_id=uuid4().hex[:12],
            provider_id=provider.identity,
            adapter_version=provider.adapter_version,
            started_at=self.clock.now(),
        )
        self._state = SessionHealth.DISCONNECTED
        self._last_sequence: int | None = None
        self._last_cycle_at: datetime | None = None
        self._seen_events: set[str] = set()
        self._open_contracts: set[str] = set()
        self._closed_this_cycle: set[str] = set()
        self.cycles: list[LiveCycleReport] = []
        self.snapshots: list[LiveSnapshot] = []
        self.last_snapshot: LiveSnapshot | None = None

    @property
    def health(self) -> LiveHealth:
        provider = self.provider.health()
        return LiveHealth(
            state=self._state,
            provider_id=self.provider.identity,
            adapter_version=self.provider.adapter_version,
            last_message_at=None if self.last_snapshot is None else self.last_snapshot.received_time,
            last_sequence=self._last_sequence,
            error=provider.error,
            reconnect_count=provider.reconnect_count,
            subscribed=provider.subscribed,
            last_heartbeat_at=provider.last_heartbeat_at,
        )

    def start(self) -> None:
        self._transition(SessionHealth.CONNECTING)
        self.provider.connect()
        if self.provider.identity not in APPROVED_STREAM_IDS:
            self._transition(SessionHealth.DEGRADED)
            raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{self.provider.identity}")
        self._transition(SessionHealth.READY)

    def stop(self) -> None:
        self.provider.disconnect()
        self.session.ended_at = self.clock.now()
        self._transition(SessionHealth.STOPPED)

    def run_once(self, underlying: str | None = None) -> list[LiveCycleReport]:
        if self._state in {SessionHealth.DISCONNECTED, SessionHealth.STOPPED, SessionHealth.CONNECTING}:
            as_of = self.clock.now()
            report = _no_trade(
                session_id=self.session.session_id,
                event_id=f"{self.session.session_id}:no-session",
                sequence=self._last_sequence or 0,
                as_of=as_of,
                underlying=underlying or "*",
                reason=f"SESSION_{self._state.value}",
                provider_id=self.provider.identity,
                health=self._state,
            )
            self.cycles.append(report)
            return [report]
        if self._session_timed_out():
            open_positions = self.positions.open_positions()
            if open_positions:
                self.positions.halt_for_timeout()
                self._transition(SessionHealth.DEGRADED)
                reason = "SESSION_TIMEOUT_WITH_OPEN_POSITION"
                extras = {
                    "open_positions": len(open_positions),
                    "unresolved_close": True,
                    "halted": True,
                    "session_summary": self.positions.summary().to_dict(),
                }
            else:
                reason = "SESSION_TIMEOUT"
                extras = {}
            self.stop()
            report = _no_trade(
                session_id=self.session.session_id,
                event_id=f"{self.session.session_id}:timeout",
                sequence=self._last_sequence or 0,
                as_of=self.clock.now(),
                underlying=underlying or "*",
                reason=reason,
                provider_id=self.provider.identity,
                health=self._state,
                extras=extras,
            )
            self.cycles.append(report)
            self.positions.note_no_trade()
            return [report]
        if self._interval_pending():
            report = _no_trade(
                session_id=self.session.session_id,
                event_id=f"{self.session.session_id}:interval",
                sequence=self._last_sequence or 0,
                as_of=self.clock.now(),
                underlying=underlying or "*",
                reason="SNAPSHOT_INTERVAL",
                provider_id=self.provider.identity,
                health=self._state,
            )
            self.cycles.append(report)
            return [report]
        try:
            raw = self.provider.poll()
        except GrowConfigError as exc:
            self._transition(SessionHealth.DEGRADED)
            report = _no_trade(
                session_id=self.session.session_id,
                event_id=f"{self.session.session_id}:provider",
                sequence=self._last_sequence or 0,
                as_of=self.clock.now(),
                underlying=underlying or "*",
                reason=str(exc),
                provider_id=self.provider.identity,
                health=self._state,
            )
            self.cycles.append(report)
            return [report]
        if raw is None:
            state = self.provider.health().state
            if state is SessionHealth.STOPPED:
                self._transition(SessionHealth.STOPPED)
            else:
                self._transition(SessionHealth.DEGRADED)
            report = _no_trade(
                session_id=self.session.session_id,
                event_id=f"{self.session.session_id}:empty",
                sequence=self._last_sequence or 0,
                as_of=self.clock.now(),
                underlying=underlying or "*",
                reason="PROVIDER_UNAVAILABLE",
                provider_id=self.provider.identity,
                health=self._state,
            )
            self.cycles.append(report)
            return [report]
        if str(raw.get("kind") or "") in CONTROL_KINDS or str(raw.get("kind") or "") == "catalog":
            report = _no_trade(
                session_id=self.session.session_id,
                event_id=f"{self.session.session_id}:control",
                sequence=self._last_sequence or 0,
                as_of=self.clock.now(),
                underlying=underlying or "*",
                reason=f"FEED_{str(raw.get('kind')).upper()}",
                provider_id=self.provider.identity,
                health=self._state,
            )
            self.cycles.append(report)
            return [report]
        try:
            snapshot = normalize_event(
                raw,
                now=self.clock.now(),
                max_staleness_seconds=self.config.live_data.max_staleness_seconds,
                calendar=self.calendar,
                last_sequence=self._last_sequence,
            )
        except GrowConfigError as exc:
            code = str(exc)
            if "OUT_OF_ORDER" in code:
                self._transition(SessionHealth.DEGRADED)
            elif "DUPLICATE_SEQUENCE" in code:
                pass
            else:
                self._transition(SessionHealth.DEGRADED)
            report = _no_trade(
                session_id=self.session.session_id,
                event_id=f"{self.session.session_id}:invalid",
                sequence=int(raw.get("sequence") or 0),
                as_of=self.clock.now(),
                underlying=underlying or "*",
                reason=code,
                provider_id=self.provider.identity,
                health=self._state,
            )
            self.cycles.append(report)
            return [report]
        self._last_sequence = snapshot.sequence
        self.last_snapshot = snapshot
        self.snapshots.append(snapshot)
        if not snapshot.freshness_ok:
            self._transition(SessionHealth.STALE)
            reports = []
            for symbol in snapshot.underlyings:
                if underlying is not None and symbol != underlying:
                    continue
                report = _no_trade(
                    session_id=self.session.session_id,
                    event_id=_event_id(self.session.session_id, snapshot, symbol, "stale"),
                    sequence=snapshot.sequence,
                    as_of=snapshot.event_time,
                    underlying=symbol,
                    reason=snapshot.diagnostics[0] if snapshot.diagnostics else "FEED_STALE",
                    provider_id=snapshot.provider_id,
                    health=self._state,
                    snapshot_id=snapshot.snapshot_id,
                )
                self.cycles.append(report)
                reports.append(report)
            self._complete_cycle()
            return reports
        self._transition(SessionHealth.RUNNING)
        self._closed_this_cycle = set()
        reports: list[LiveCycleReport] = list(self._manage_positions(snapshot, underlying))
        if self._allows_new_entries(snapshot):
            targets = [underlying] if underlying else list(snapshot.underlyings)
            for symbol in targets:
                reports.append(self._evaluate(symbol, snapshot))
        elif not reports:
            report = _no_trade(
                session_id=self.session.session_id,
                event_id=_event_id(self.session.session_id, snapshot, underlying or "*", "blocked"),
                sequence=snapshot.sequence,
                as_of=snapshot.event_time,
                underlying=underlying or "*",
                reason="NEW_ENTRIES_BLOCKED",
                provider_id=snapshot.provider_id,
                health=self._state,
                snapshot_id=snapshot.snapshot_id,
                extras={"session_summary": self.positions.summary().to_dict()},
            )
            self.cycles.append(report)
            self.positions.note_no_trade()
            reports.append(report)
        self._complete_cycle()
        return reports

    def _evaluate(self, symbol: str, snapshot: LiveSnapshot) -> LiveCycleReport:
        event_id = _event_id(self.session.session_id, snapshot, symbol, "cycle")
        base = dict(
            session_id=self.session.session_id,
            event_id=event_id,
            sequence=snapshot.sequence,
            as_of=snapshot.event_time,
            underlying=symbol,
            provider_id=snapshot.provider_id,
            health=self._state,
            snapshot_id=snapshot.snapshot_id,
        )
        if event_id in self._seen_events:
            report = _no_trade(**base, reason="DUPLICATE_EVENT")
            self.cycles.append(report)
            return report
        registry = default_index_registry()
        allowed = self.config.live_data.allowed_underlyings
        if allowed and symbol not in allowed:
            report = _no_trade(**base, reason=f"UNDERLYING_NOT_ALLOWED:{symbol}")
            self.cycles.append(report)
            return report
        if not registry.allows(symbol, snapshot.event_time.date()):
            report = _no_trade(**base, reason=f"UNSUPPORTED_UNDERLYING:{symbol}")
            self.cycles.append(report)
            return report
        market = snapshot.market.get(symbol)
        chain = snapshot.chains.get(symbol)
        if market is None:
            report = _no_trade(**base, reason="MISSING_MARKET")
            self.cycles.append(report)
            return report
        if self.config.live_data.require_option_chain and (chain is None or not chain.contracts):
            report = _no_trade(**base, reason="MISSING_OPTION_CHAIN")
            self.cycles.append(report)
            return report
        result = self.strategies.evaluate(market)
        signal = pick_signal(tuple(result.signals))
        if signal is None:
            why = result.skipped[0].reason if result.skipped else "NO_SIGNAL"
            report = _no_trade(**base, reason=why)
            self.cycles.append(report)
            return report
        options = self.options.evaluate(signal, market, chain)
        if options.status is not DecisionStatus.CANDIDATE or options.candidate is None:
            why = options.diagnostics[0] if options.diagnostics else "OPTIONS_NO_TRADE"
            report = _no_trade(**base, reason=why, candidate_id=None)
            self.cycles.append(report)
            return report
        candidate = options.candidate
        lot = snapshot.lot_sizes.get(candidate.candidate_id) or snapshot.lot_sizes.get(candidate.contract_symbol)
        ident = f"{candidate.underlying}-{candidate.expiry.isoformat()}-{int(candidate.strike)}-{candidate.option_type}"
        if lot is None:
            lot = snapshot.lot_sizes.get(ident)
        if lot is None or lot < 1:
            report = _no_trade(**base, reason="MISSING_LOT_SIZE", candidate_id=candidate.candidate_id)
            self.cycles.append(report)
            return report
        candidate = replace(candidate, lot_size=lot)
        if signal.direction == "BULLISH" and candidate.option_type != "CE":
            report = _no_trade(**base, reason="BULLISH_NOT_CE", candidate_id=candidate.candidate_id)
            self.cycles.append(report)
            return report
        if signal.direction == "BEARISH" and candidate.option_type != "PE":
            report = _no_trade(**base, reason="BEARISH_NOT_PE", candidate_id=candidate.candidate_id)
            self.cycles.append(report)
            return report
        packet = build_packet(
            view=research_view(market),
            signal=signal,
            options=replace(options, candidate=candidate),
            configuration_version=self.config.version,
        )
        ceo = self.research.run(packet)
        if ceo.decision is not CEOVerdict.TRADE_APPROVE:
            why = ceo.rejection_reasons[0] if ceo.rejection_reasons else "CEO_NO_TRADE"
            report = _no_trade(**base, reason=why, candidate_id=candidate.candidate_id, decision_id=ceo.decision_id)
            self.cycles.append(report)
            return report
        if ident in self._open_contracts or ident in self._closed_this_cycle or self.positions.has_open(ident):
            report = _no_trade(
                **base,
                reason="DUPLICATE_OPEN_POSITION",
                candidate_id=candidate.candidate_id,
                decision_id=ceo.decision_id,
            )
            self.cycles.append(report)
            self.positions.note_no_trade()
            return report
        filled = self.simulator.enter(candidate, as_of=snapshot.event_time)
        if isinstance(filled, str):
            report = _no_trade(**base, reason=filled, candidate_id=candidate.candidate_id, decision_id=ceo.decision_id)
            self.cycles.append(report)
            return report
        lots = self.config.live_data.quantity
        quantity = lots * lot
        ticker = ident
        sl_pct = self.config.live_data.stop_loss_pct
        tp_pct = self.config.live_data.take_profit_pct
        self._ensure_universe(ticker)
        proposal = TradeProposal(
            proposal_id=f"lp-{ceo.decision_id}",
            symbol=Symbol(ticker),
            side=Side.BUY,
            intent=Intent.OPEN,
            quantity=quantity,
            limit_price=filled.price,
            stop_loss=round(filled.price * (1.0 - sl_pct), 2),
            take_profit=round(filled.price * (1.0 + tp_pct), 2),
            thesis=ceo.rationale_summary,
            confidence=ceo.confidence,
            venue=Venue.PAPER,
            created_at=snapshot.event_time,
            notional=expected_notional(quantity, filled.price),
            extras={
                "event_id": event_id,
                "candidate_id": candidate.candidate_id,
                "decision_id": ceo.decision_id,
                "provider_id": snapshot.provider_id,
                "lot_size": lot,
                "lots": lots,
            },
        )
        brief = MarketBrief(
            symbol=proposal.symbol,
            as_of=snapshot.event_time,
            session=market.session,
            last_price=filled.price,
            currency="INR",
            regime=Regime.TRENDING_UP if signal.direction == "BULLISH" else Regime.TRENDING_DOWN,
            source=snapshot.provider_id,
        )
        book = self.ledger.book
        verdict = self.guard.evaluate(
            proposal,
            brief,
            cash=book.cash,
            gross_notional=book.gross_notional,
            daily_pnl=self.positions.summary().net_realized_pnl,
            symbol_notional=book.symbol_notional(ticker),
        )
        if not verdict.approved:
            report = _no_trade(
                **base,
                reason=f"RISK_GUARD:{verdict.reason}",
                candidate_id=candidate.candidate_id,
                decision_id=ceo.decision_id,
            )
            report = replace(report, verdict=verdict)
            self.cycles.append(report)
            return report
        fill = self.ledger.submit(proposal, verdict.stamp)
        self._seen_events.add(event_id)
        self._open_contracts.add(ident)
        position = self.positions.register_open(
            session_id=self.session.session_id,
            candidate_id=candidate.candidate_id,
            contract_id=ident,
            underlying=symbol,
            expiry=candidate.expiry,
            strike=candidate.strike,
            option_type=str(candidate.option_type),
            provider_id=snapshot.provider_id,
            lot_size=lot,
            lots=lots,
            quantity=quantity,
            entry_price=fill.price,
            opened_at=fill.filled_at,
            stop_loss_price=proposal.stop_loss or round(fill.price * (1.0 - sl_pct), 2),
            take_profit_price=proposal.take_profit or round(fill.price * (1.0 + tp_pct), 2),
            fill_id=fill.fill_id,
            snapshot_id=snapshot.snapshot_id,
        )
        report = LiveCycleReport(
            session_id=self.session.session_id,
            event_id=event_id,
            sequence=snapshot.sequence,
            as_of=snapshot.event_time,
            underlying=symbol,
            status=CycleStatus.PAPER_FILL,
            reason="PAPER_FILL",
            provider_id=snapshot.provider_id,
            snapshot_id=snapshot.snapshot_id,
            candidate_id=candidate.candidate_id,
            decision_id=ceo.decision_id,
            option_type=candidate.option_type,
            expiry=candidate.expiry.isoformat(),
            strike=candidate.strike,
            lots=lots,
            lot_size=lot,
            fill=fill,
            verdict=verdict,
            health=self._state,
            extras={
                "gross_notional": fill.notional,
                "costs_model": self.costs.version,
                "entry_reference": filled.reference,
                "pnl_formula": "price_delta * lots * lot_size",
                "contract_pnl_if_flat": contract_pnl(entry=fill.price, exit=fill.price, lots=lots, lot_size=lot),
                "position_id": position.position_id,
                "stop_loss_price": position.stop_loss_price,
                "take_profit_price": position.take_profit_price,
                "session_summary": self.positions.summary().to_dict(),
            },
        )
        self.cycles.append(report)
        return report

    def _allows_new_entries(self, snapshot: LiveSnapshot) -> bool:
        if self._state in {SessionHealth.STALE, SessionHealth.DEGRADED, SessionHealth.STOPPED}:
            return False
        if self.positions.halted or self.positions.unresolved_close:
            return False
        return self.calendar.allows_new_entries(snapshot.event_time)

    def _manage_positions(self, snapshot: LiveSnapshot, underlying: str | None) -> list[LiveCycleReport]:
        del underlying
        reports: list[LiveCycleReport] = []
        self.positions.mark(snapshot)
        for decision in self.positions.exits(snapshot):
            reports.append(self._close_position(decision, snapshot))
        if self.positions.halted:
            self._transition(SessionHealth.DEGRADED)
        return reports

    def _close_position(self, decision: ExitDecision, snapshot: LiveSnapshot) -> LiveCycleReport:
        position = self.positions.get(decision.position_id)
        event_id = f"{self.session.session_id}:{snapshot.sequence}:{decision.position_id}:close"
        base = dict(
            session_id=self.session.session_id,
            event_id=event_id,
            sequence=snapshot.sequence,
            as_of=snapshot.event_time,
            underlying=position.underlying if position else "*",
            provider_id=snapshot.provider_id,
            health=self._state,
            snapshot_id=snapshot.snapshot_id,
            candidate_id=None if position is None else position.candidate_id,
        )
        if position is None or position.state is PositionState.CLOSED:
            report = _no_trade(**base, reason="DUPLICATE_CLOSE")
            self.cycles.append(report)
            return report
        pending = self.positions.begin_exit(position.position_id)
        if pending is None:
            report = _no_trade(**base, reason="DUPLICATE_CLOSE")
            self.cycles.append(report)
            return report
        filled = self.simulator.exit(None, as_of=snapshot.event_time, reason=decision.reason, fallback_bid=decision.mark_price)
        if isinstance(filled, str):
            self.positions.abort_exit(position.position_id, filled)
            if decision.reason == ExitReason.SESSION_CLOSE:
                self.positions.unresolved_close = True
                self.positions.halted = True
                self._transition(SessionHealth.DEGRADED)
            report = _no_trade(
                **base,
                reason=filled,
                extras={"position_id": position.position_id, "exit_reason": decision.reason},
            )
            self.cycles.append(report)
            return report
        ticker = position.contract_id
        self._ensure_universe(ticker)
        intent = Intent.SQUARE_OFF if decision.reason == ExitReason.SESSION_CLOSE else Intent.CLOSE
        proposal = TradeProposal(
            proposal_id=f"px-{position.position_id}-{decision.reason}",
            symbol=Symbol(ticker),
            side=Side.SELL,
            intent=intent,
            quantity=position.quantity,
            limit_price=filled.price,
            stop_loss=None,
            take_profit=None,
            thesis=f"3B deterministic {decision.reason}",
            confidence=1.0,
            venue=Venue.PAPER,
            created_at=snapshot.event_time,
            notional=expected_notional(position.quantity, filled.price),
            extras={
                "position_id": position.position_id,
                "exit_reason": decision.reason,
                "lot_size": position.lot_size,
                "lots": position.lots,
            },
        )
        brief = MarketBrief(
            symbol=proposal.symbol,
            as_of=snapshot.event_time,
            session=self.calendar.state(snapshot.event_time),
            last_price=filled.price,
            currency="INR",
            regime=Regime.UNKNOWN,
            source=snapshot.provider_id,
        )
        book = self.ledger.book
        verdict = self.guard.evaluate_exit(
            proposal,
            brief,
            cash=book.cash,
            gross_notional=book.gross_notional,
            daily_pnl=self.positions.summary().net_realized_pnl,
            symbol_notional=book.symbol_notional(ticker),
        )
        if not verdict.approved:
            self.positions.abort_exit(position.position_id, f"RISK_GUARD:{verdict.reason}")
            if decision.reason == ExitReason.SESSION_CLOSE:
                self.positions.unresolved_close = True
                self.positions.halted = True
                self._transition(SessionHealth.DEGRADED)
            report = _no_trade(**base, reason=f"RISK_GUARD:{verdict.reason}", extras={"position_id": position.position_id})
            report = replace(report, verdict=verdict)
            self.cycles.append(report)
            return report
        try:
            fill = self.ledger.submit(proposal, verdict.stamp)
        except Exception as exc:
            self.positions.abort_exit(position.position_id, f"LEDGER:{exc}")
            report = _no_trade(**base, reason=f"LEDGER:{exc}", extras={"position_id": position.position_id})
            self.cycles.append(report)
            return report
        if fill.side is Side.SELL and fill.quantity > position.quantity:
            raise RuntimeError("exit quantity exceeded open quantity")
        costs = self.costs.round_trip(
            entry=position.entry_price,
            exit=fill.price,
            quantity=position.lots,
            lot_size=position.lot_size,
        )
        closed = self.positions.complete_close(
            position.position_id,
            exit_reason=decision.reason,
            exit_price=fill.price,
            closed_at=fill.filled_at,
            costs=costs,
            close_fill_id=fill.fill_id,
            snapshot_id=snapshot.snapshot_id,
        )
        self._open_contracts.discard(position.contract_id)
        self._closed_this_cycle.add(position.contract_id)
        report = LiveCycleReport(
            session_id=self.session.session_id,
            event_id=event_id,
            sequence=snapshot.sequence,
            as_of=snapshot.event_time,
            underlying=position.underlying,
            status=CycleStatus.PAPER_CLOSE,
            reason=decision.reason,
            provider_id=snapshot.provider_id,
            snapshot_id=snapshot.snapshot_id,
            candidate_id=position.candidate_id,
            decision_id=None,
            option_type=position.option_type,
            expiry=position.expiry.isoformat(),
            strike=position.strike,
            lots=position.lots,
            lot_size=position.lot_size,
            fill=fill,
            verdict=verdict,
            health=self._state,
            extras={
                "position_id": closed.position_id,
                "exit_reason": closed.exit_reason,
                "exit_price": fill.price,
                "quantity": closed.quantity,
                "gross_pnl": closed.realized_gross,
                "costs": closed.total_costs,
                "net_pnl": closed.realized_pnl,
                "price_source": decision.price_source,
                "session_summary": self.positions.summary().to_dict(),
            },
        )
        self.cycles.append(report)
        return report

    def _session_timed_out(self) -> bool:
        elapsed = (self.clock.now() - self.session.started_at).total_seconds()
        return elapsed >= self.config.live_data.session_timeout_seconds

    def _interval_pending(self) -> bool:
        interval = self.config.live_data.snapshot_interval_seconds
        if interval <= 0 or self._last_cycle_at is None:
            return False
        elapsed = (self.clock.now() - self._last_cycle_at).total_seconds()
        return elapsed < interval

    def _complete_cycle(self) -> None:
        self._last_cycle_at = self.clock.now()

    def _ensure_universe(self, ticker: str) -> None:
        current = self.guard.config.market.universe
        if ticker in current:
            return
        cfg = replace(self.guard.config, market=replace(self.guard.config.market, universe=(*current, ticker)))
        self.guard = RiskGuard(cfg, clock=self.clock, secret=self._risk_secret)
        self.ledger.guard = self.guard
        self.ledger.config = cfg

    def _transition(self, state: SessionHealth) -> None:
        previous = self._state
        if previous is state:
            return
        self.session.transitions.append((self.clock.now().isoformat(), previous.value, state.value))
        self._state = state


def _event_id(session_id: str, snapshot: LiveSnapshot, symbol: str, suffix: str) -> str:
    return f"{session_id}:{snapshot.sequence}:{symbol}:{suffix}"


def open_loop(config: GrowConfig, *, clock: Clock | None = None, risk_secret: str | None = None, events=None) -> LivePaperLoop:
    kwargs: dict[str, Any] = {}
    if events is not None:
        kwargs["events"] = events
    if config.live_data.provider == "truedata":
        from grow.live_data.truedata import settings_from_live_config

        kwargs["settings"] = settings_from_live_config(config.live_data)
        kwargs["clock"] = clock
        kwargs["live_config"] = config.live_data
    provider = open_provider(config.live_data.provider, **kwargs)
    return LivePaperLoop(config, provider, clock=clock, risk_secret=risk_secret)
