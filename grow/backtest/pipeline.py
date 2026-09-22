"""One historical decision: 2B → 2C → 2D (or ablation) → fill/exit."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from grow.backtest.calendar import SQUARE_OFF, at_session
from grow.backtest.costs import CostModel, contract_pnl
from grow.backtest.ledger import BacktestLedger
from grow.backtest.models import BacktestTrade, DecisionRow, SimulatedFill
from grow.backtest.simulate import ExecutionSimulator
from grow.data.boundary import research_view
from grow.data.hub import DataHub
from grow.data.schema import Timeframe
from grow.options.engine import IndexOptionsEngine
from grow.options.models import DecisionStatus, OptionCandidate, OptionContract
from grow.options.source import OptionChainSource
from grow.research.models import CEODecision, CEOVerdict
from grow.research.orchestrator import ResearchOrchestrator
from grow.research.packet import build_packet
from grow.research.validate import no_trade
from grow.strategies.engine import StrategyEngine
from grow.strategies.signal import StrategySignal


def pick_signal(signals: tuple[StrategySignal, ...]) -> StrategySignal | None:
    if not signals:
        return None
    ranked = sorted(signals, key=lambda item: (-item.confidence, item.strategy, item.signal_id))
    return ranked[0]


def _find_contract(contracts: tuple[OptionContract, ...], candidate: OptionCandidate) -> OptionContract | None:
    for contract in contracts:
        if (
            contract.underlying == candidate.underlying
            and contract.expiry == candidate.expiry
            and abs(contract.strike - candidate.strike) < 1e-9
            and contract.option_type.value == candidate.option_type
        ):
            return contract
    return None


def historical_store_of(hub, chains):
    store = getattr(chains, "store", None)
    if store is not None:
        return store
    source = getattr(hub, "source", None)
    return getattr(source, "store", None)


def bind_execution_lot(candidate: OptionCandidate, *, store, fixture_lot_size: int) -> OptionCandidate | str:
    """Historical P&L uses canonical contract lot_size. Fixture uses configured lot size."""
    if store is None or getattr(getattr(store, "meta", None), "is_fixture", True):
        lot = candidate.lot_size if candidate.lot_size is not None and candidate.lot_size >= 1 else fixture_lot_size
        if lot < 1:
            return "MISSING_LOT_SIZE"
        return replace(candidate, lot_size=lot)
    resolved = store.contract_for_candidate(
        underlying=candidate.underlying,
        expiry=candidate.expiry,
        strike=candidate.strike,
        option_type=candidate.option_type,
        as_of=candidate.as_of,
        provider_contract_id=candidate.contract_symbol,
    )
    if resolved is None or resolved.lot_size is None or resolved.lot_size < 1:
        return "MISSING_LOT_SIZE"
    return replace(candidate, lot_size=resolved.lot_size)


def _path_exit(signal: StrategySignal, bars, decision_as_of: datetime) -> tuple[datetime | None, str]:
    later = [bar for bar in bars if bar.start >= decision_as_of]
    for bar in later:
        if signal.direction == "BULLISH":
            stop_hit = bar.low <= signal.stop
            target_hit = bar.high >= signal.target
        else:
            stop_hit = bar.high >= signal.stop
            target_hit = bar.low <= signal.target
        if stop_hit and target_hit:
            return bar.end, "STOP_FIRST_AMBIGUOUS"
        if stop_hit:
            return bar.end, "STOP"
        if target_hit:
            return bar.end, "TARGET"
    return None, "SQUARE_OFF"


class DecisionPipeline:
    def __init__(
        self,
        *,
        hub: DataHub,
        strategies: StrategyEngine,
        options: IndexOptionsEngine,
        chains: OptionChainSource,
        research: ResearchOrchestrator,
        simulator: ExecutionSimulator,
        costs: CostModel,
        ledger: BacktestLedger,
        run_id: str,
        ablation: str = "full",
        config_version: str,
        recorded: dict[str, CEODecision] | None = None,
        lot_size: int = 1,
    ) -> None:
        self.hub = hub
        self.strategies = strategies
        self.options = options
        self.chains = chains
        self.research = research
        self.simulator = simulator
        self.costs = costs
        self.ledger = ledger
        self.run_id = run_id
        self.ablation = ablation
        self.config_version = config_version
        self.recorded = recorded
        self.lot_size = lot_size

    def evaluate(self, ticker: str, as_of: datetime) -> None:
        if self.ablation == "always_no_trade":
            self.ledger.record_decision(
                DecisionRow(as_of, ticker, "NO_TRADE", "ABLATION_ALWAYS_NO_TRADE", None, None, None, self.ablation)
            )
            return
        snap = self.hub.snapshot(ticker, as_of=as_of)
        for series in snap.series.values():
            if series.bars and series.bars[-1].end > as_of:
                self.ledger.record_decision(
                    DecisionRow(as_of, ticker, "NO_TRADE", "LOOKAHEAD_UNDERLYING", None, None, None, self.ablation)
                )
                return
        result = self.strategies.evaluate(snap)
        signal = pick_signal(tuple(result.signals))
        if signal is None:
            self.ledger.record_decision(
                DecisionRow(as_of, ticker, "NO_TRADE", "NO_SIGNAL", None, None, None, self.ablation)
            )
            return
        chain = self.chains.snapshot(ticker, as_of, spot=snap.last_price)
        if chain.as_of > as_of:
            self.ledger.record_decision(
                DecisionRow(as_of, ticker, "NO_TRADE", "LOOKAHEAD_CHAIN", signal.signal_id, None, None, self.ablation)
            )
            return
        if chain.as_of != as_of:
            self.ledger.record_decision(
                DecisionRow(as_of, ticker, "NO_TRADE", "ASOF_MISMATCH", signal.signal_id, None, None, self.ablation)
            )
            return
        options = self.options.evaluate(signal, snap, chain)
        if options.status is not DecisionStatus.CANDIDATE or options.candidate is None:
            why = options.diagnostics[0] if options.diagnostics else "OPTIONS_NO_TRADE"
            self.ledger.record_decision(
                DecisionRow(as_of, ticker, "NO_TRADE", why, signal.signal_id, None, None, self.ablation)
            )
            return
        candidate = options.candidate
        bound = bind_execution_lot(
            candidate,
            store=historical_store_of(self.hub, self.chains),
            fixture_lot_size=self.lot_size,
        )
        if isinstance(bound, str):
            self.ledger.record_decision(
                DecisionRow(as_of, ticker, "NO_TRADE", bound, signal.signal_id, candidate.candidate_id, None, self.ablation)
            )
            return
        options = replace(options, candidate=bound)
        candidate = bound
        if signal.direction == "BULLISH" and candidate.option_type != "CE":
            self.ledger.record_decision(
                DecisionRow(as_of, ticker, "NO_TRADE", "BULLISH_PE", signal.signal_id, candidate.candidate_id, None, self.ablation)
            )
            return
        if signal.direction == "BEARISH" and candidate.option_type != "PE":
            self.ledger.record_decision(
                DecisionRow(as_of, ticker, "NO_TRADE", "BEARISH_CE", signal.signal_id, candidate.candidate_id, None, self.ablation)
            )
            return
        decision_id = None
        packet_id = ""
        if self.ablation != "skip_ceo":
            packet = build_packet(
                view=research_view(snap),
                signal=signal,
                options=options,
                configuration_version=self.config_version,
            )
            packet_id = packet.packet_id
            ceo = resolve_ceo(packet, self.research, self.recorded)
            decision_id = ceo.decision_id
            if ceo.decision is not CEOVerdict.TRADE_APPROVE:
                why = ceo.rejection_reasons[0] if ceo.rejection_reasons else "CEO_NO_TRADE"
                self.ledger.record_decision(
                    DecisionRow(as_of, ticker, "NO_TRADE", why, signal.signal_id, candidate.candidate_id, decision_id, self.ablation)
                )
                return
            if ceo.selected_candidate_id != candidate.candidate_id:
                self.ledger.record_decision(
                    DecisionRow(as_of, ticker, "NO_TRADE", "UNKNOWN_CANDIDATE", signal.signal_id, candidate.candidate_id, decision_id, self.ablation)
                )
                return
        entry = self.simulator.enter(candidate, as_of=as_of)
        if isinstance(entry, str):
            self.ledger.record_decision(
                DecisionRow(as_of, ticker, "NO_TRADE", entry, signal.signal_id, candidate.candidate_id, decision_id, self.ablation)
            )
            return
        self._close(ticker, as_of, signal, candidate, entry, snap.snapshot_id, chain.snapshot_id, decision_id, packet_id)

    def _close(
        self,
        ticker: str,
        as_of: datetime,
        signal: StrategySignal,
        candidate: OptionCandidate,
        entry: SimulatedFill,
        market_id: str,
        chain_id: str,
        decision_id: str | None,
        packet_id: str,
    ) -> None:
        square = at_session(as_of.date(), SQUARE_OFF)
        exit_snap = self.hub.snapshot(ticker, as_of=square)
        m15 = exit_snap.series.get(Timeframe.M15)
        bars = m15.bars if m15 is not None else ()
        when, reason = _path_exit(signal, bars, as_of)
        exit_at = when or square
        if exit_at > square:
            exit_at = square
            reason = "SQUARE_OFF"
        exit_chain = self.chains.snapshot(ticker, exit_at, spot=self.hub.snapshot(ticker, as_of=exit_at).last_price)
        contract = _find_contract(exit_chain.contracts, candidate)
        filled = self.simulator.exit(contract, as_of=exit_at, reason=reason)
        flags: list[str] = []
        if reason == "STOP_FIRST_AMBIGUOUS":
            flags.append("STOP_FIRST_AMBIGUOUS")
        if isinstance(filled, str):
            self.ledger.record_decision(
                DecisionRow(as_of, ticker, "NO_TRADE", filled, signal.signal_id, candidate.candidate_id, decision_id, self.ablation)
            )
            return
        lots = entry.quantity
        lot = candidate.lot_size
        if lot is None or lot < 1:
            self.ledger.record_decision(
                DecisionRow(as_of, ticker, "NO_TRADE", "MISSING_LOT_SIZE", signal.signal_id, candidate.candidate_id, decision_id, self.ablation)
            )
            return
        gross = contract_pnl(entry=entry.price, exit=filled.price, lots=lots, lot_size=lot)
        cost = self.costs.round_trip(entry=entry.price, exit=filled.price, quantity=lots, lot_size=lot)
        net = round(gross - cost, 4)
        trade = BacktestTrade(
            trade_id=f"{self.run_id}:{ticker}:{as_of.isoformat()}:{candidate.candidate_id[:8]}",
            run_id=self.run_id,
            underlying=ticker,
            decision_as_of=as_of,
            strategy_signal_id=signal.signal_id,
            strategy_version=signal.strategy_version,
            direction=signal.direction,
            option_candidate_id=candidate.candidate_id,
            expiry=candidate.expiry.isoformat(),
            strike=candidate.strike,
            option_type=candidate.option_type,
            entry_reference=entry.reference,
            entry_fill=entry.price,
            quantity=lots,
            lot_size=lot,
            exit_reason=reason,
            exit_timestamp=exit_at,
            exit_fill=filled.price,
            gross_pnl=gross,
            total_cost=cost,
            net_pnl=net,
            data_quality_flags=tuple(flags),
            research_decision_id=decision_id or "skip_ceo",
            packet_id=packet_id,
            source_snapshot_ids={"market": market_id, "chain": chain_id},
            primary=True,
        )
        self.ledger.record_trade(trade)
        self.ledger.record_decision(
            DecisionRow(as_of, ticker, "FILL", reason, signal.signal_id, candidate.candidate_id, decision_id, self.ablation)
        )


def resolve_ceo(
    packet,
    research: ResearchOrchestrator,
    recorded: dict[str, CEODecision] | None,
) -> CEODecision:
    """Use a recorded CEODecision when supplied. Do not call orchestrator.run then."""
    if recorded is None:
        return research.run(packet)
    replay = recorded.get(packet.packet_id)
    if replay is None:
        return no_trade(packet, reasons=("MISSING_RECORDED_AI",), validation_ok=True)
    reports = tuple(agent.research(packet) for agent in research.agents)
    return research.validator.validate(packet, replay, reports)
