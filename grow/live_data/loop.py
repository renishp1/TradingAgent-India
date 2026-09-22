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
    MOCK_PROVIDER_ID,
    CycleStatus,
    LiveCycleReport,
    LiveHealth,
    LiveSessionRecord,
    LiveSnapshot,
    SessionHealth,
)
from grow.live_data.normalize import normalize_event
from grow.live_data.provider import LiveDataProvider, open_provider
from grow.market.session import SessionCalendar
from grow.options.engine import IndexOptionsEngine
from grow.options.models import DecisionStatus, OptionCandidate, OptionCandidate
from grow.paper.ledger import PaperLedger, expected_notional
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
        self.cycles: list[LiveCycleReport] = []
        self.snapshots: list[LiveSnapshot] = []
        self.last_snapshot: LiveSnapshot | None = None

    @property
    def health(self) -> LiveHealth:
        return LiveHealth(
            state=self._state,
            provider_id=self.provider.identity,
            adapter_version=self.provider.adapter_version,
            last_message_at=None if self.last_snapshot is None else self.last_snapshot.received_time,
            last_sequence=self._last_sequence,
            error=self.provider.health().error,
        )

    def start(self) -> None:
        self._transition(SessionHealth.CONNECTING)
        self.provider.connect()
        if self.provider.identity != MOCK_PROVIDER_ID:
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
            self.stop()
            report = _no_trade(
                session_id=self.session.session_id,
                event_id=f"{self.session.session_id}:timeout",
                sequence=self._last_sequence or 0,
                as_of=self.clock.now(),
                underlying=underlying or "*",
                reason="SESSION_TIMEOUT",
                provider_id=self.provider.identity,
                health=self._state,
            )
            self.cycles.append(report)
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
        targets = [underlying] if underlying else list(snapshot.underlyings)
        reports: list[LiveCycleReport] = []
        for symbol in targets:
            reports.append(self._evaluate(symbol, snapshot))
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
        if ident in self._open_contracts:
            report = _no_trade(
                **base,
                reason="DUPLICATE_OPEN_POSITION",
                candidate_id=candidate.candidate_id,
                decision_id=ceo.decision_id,
            )
            self.cycles.append(report)
            return report
        filled = self.simulator.enter(candidate, as_of=snapshot.event_time)
        if isinstance(filled, str):
            report = _no_trade(**base, reason=filled, candidate_id=candidate.candidate_id, decision_id=ceo.decision_id)
            self.cycles.append(report)
            return report
        lots = self.config.live_data.quantity
        quantity = lots * lot
        ticker = ident
        self._ensure_universe(ticker)
        proposal = TradeProposal(
            proposal_id=f"lp-{ceo.decision_id}",
            symbol=Symbol(ticker),
            side=Side.BUY,
            intent=Intent.OPEN,
            quantity=quantity,
            limit_price=filled.price,
            stop_loss=round(filled.price * 0.8, 2),
            take_profit=round(filled.price * 1.2, 2),
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
            daily_pnl=book.realized_pnl,
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
    provider = open_provider(config.live_data.provider, events=events)
    return LivePaperLoop(config, provider, clock=clock, risk_secret=risk_secret)
