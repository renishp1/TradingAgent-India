"""4C decision integration. Risk Guard is the final safety authority.

Pipeline: validated 4B package → evidence/conflict policy → Risk Guard →
immutable audit record. A CANDIDATE is a paper-trade candidate only.
This module does not submit paper fills and does not call a broker.
"""

from __future__ import annotations

from dataclasses import dataclass

from grow.clock import FrozenClock
from grow.config import GrowConfig
from grow.decision.integration.contract import (
    DECISION_SCHEMA,
    DecisionBookState,
    IntegratedDecision,
    IntegratedDecisionStatus,
    digest_payload,
)
from grow.decision.integration.policy import PolicyResult, evaluate_policy
from grow.errors import GrowConfigError, GrowLiveTradingDisabled, GrowSafetyError
from grow.execution.lock import assert_paper_runtime
from grow.market.session import SessionCalendar
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.orchestration.models import AggregateAnalysisPackage
from grow.risk.guard import RiskGuard
from grow.risk.secret import resolve_risk_secret
from grow.types import Intent, MarketBrief, Regime, Side, Symbol, TradeProposal, Venue


@dataclass(frozen=True)
class DecisionAuditRecord:
    decision: IntegratedDecision
    snapshot: AgentMarketSnapshot
    package: AggregateAnalysisPackage
    book: DecisionBookState


class DecisionAuditLog:
    """Append-only decision history. Records are never overwritten."""

    def __init__(self) -> None:
        self._records: list[DecisionAuditRecord] = []
        self._by_id: dict[str, DecisionAuditRecord] = {}

    def append(
        self,
        decision: IntegratedDecision,
        *,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
        book: DecisionBookState,
    ) -> IntegratedDecision:
        existing = self._by_id.get(decision.decision_id)
        record = DecisionAuditRecord(
            decision=decision,
            snapshot=snapshot,
            package=package,
            book=book,
        )
        if existing is None:
            self._records.append(record)
            self._by_id[decision.decision_id] = record
            return decision
        if existing.decision.to_dict() != decision.to_dict():
            raise GrowSafetyError(
                f"decision audit is immutable; refusing overwrite of {decision.decision_id}"
            )
        return existing.decision

    def get(self, decision_id: str) -> DecisionAuditRecord:
        try:
            return self._by_id[decision_id]
        except KeyError as exc:
            raise GrowSafetyError(f"UNKNOWN_DECISION:{decision_id}") from exc

    def __len__(self) -> int:
        return len(self._records)

    def decision_ids(self) -> tuple[str, ...]:
        return tuple(row.decision.decision_id for row in self._records)


class DecisionIntegrator:
    """Turn one 4B analysis package into one auditable decision."""

    def __init__(
        self,
        config: GrowConfig,
        *,
        risk_guard: RiskGuard | None = None,
        audit: DecisionAuditLog | None = None,
        risk_secret: str | None = None,
    ) -> None:
        assert_paper_runtime(config.execution.mode, config.execution.live_trading_enabled, "PAPER")
        self.config = config
        self._risk_guard = risk_guard
        self._risk_secret = risk_secret
        self.audit = audit or DecisionAuditLog()
        self._limits = _limit_tuple(config)

    def integrate(
        self,
        *,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
        book: DecisionBookState | None = None,
    ) -> IntegratedDecision:
        assert_paper_runtime(self.config.execution.mode, self.config.execution.live_trading_enabled, "PAPER")
        if _limit_tuple(self.config) != self._limits:
            raise GrowSafetyError("Risk Guard limits changed at runtime")
        state = book or DecisionBookState(
            cash=self.config.paper.starting_cash,
            gross_notional=0.0,
            daily_pnl=0.0,
            symbol_notional=0.0,
            open_positions=0,
        )
        decision = self._decide(snapshot, package, state)
        return self.audit.append(decision, snapshot=snapshot, package=package, book=state)

    def replay(self, decision_id: str) -> IntegratedDecision:
        """Recompute a stored decision from its snapshot, package, and book."""
        stored = self.audit.get(decision_id)
        recomputed = self._decide(stored.snapshot, stored.package, stored.book)
        if recomputed.to_dict() != stored.decision.to_dict():
            raise GrowSafetyError(f"REPLAY_DIVERGED:{decision_id}")
        return stored.decision

    def _decide(
        self,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
        book: DecisionBookState,
    ) -> IntegratedDecision:
        if snapshot.live_trading or not snapshot.paper_mode or package.live_trading or not package.paper_mode:
            return self._finish(
                snapshot,
                package,
                book,
                policy=_bind_failure(snapshot, package, "PAPER_LIVE_LOCK", "BLOCKED"),
                status=IntegratedDecisionStatus.BLOCKED,
                reasons=("PAPER_LIVE_LOCK",),
                risk_result="NOT_EVALUATED",
                risk_reason="PAPER_LIVE_LOCK",
                rules=(),
            )
        if package.snapshot_id != snapshot.snapshot_id or package.snapshot_version != snapshot.version:
            return self._finish(
                snapshot,
                package,
                book,
                policy=_bind_failure(snapshot, package, "SNAPSHOT_MISMATCH", "BLOCKED"),
                status=IntegratedDecisionStatus.BLOCKED,
                reasons=("SNAPSHOT_MISMATCH",),
                risk_result="NOT_EVALUATED",
                risk_reason="SNAPSHOT_MISMATCH",
                rules=(),
            )
        if not package.cycle_id:
            return self._finish(
                snapshot,
                package,
                book,
                policy=_bind_failure(snapshot, package, "INVALID_CYCLE", "BLOCKED"),
                status=IntegratedDecisionStatus.BLOCKED,
                reasons=("INVALID_CYCLE",),
                risk_result="NOT_EVALUATED",
                risk_reason="INVALID_CYCLE",
                rules=(),
            )

        policy = evaluate_policy(snapshot, package)
        if policy.terminal_status == "BLOCKED":
            return self._finish(
                snapshot,
                package,
                book,
                policy=policy,
                status=IntegratedDecisionStatus.BLOCKED,
                reasons=policy.reason_codes,
                risk_result="NOT_EVALUATED",
                risk_reason=policy.reason_codes[0],
                rules=(),
            )
        if policy.terminal_status == "NO_TRADE" or policy.candidate is None:
            return self._finish(
                snapshot,
                package,
                book,
                policy=policy,
                status=IntegratedDecisionStatus.NO_TRADE,
                reasons=policy.reason_codes or ("NO_VALID_STRATEGY_CANDIDATE",),
                risk_result="NOT_EVALUATED",
                risk_reason="NOT_EVALUATED",
                rules=(),
            )

        guard = self._guard_for(snapshot.decision_timestamp)
        if guard is None:
            return self._finish(
                snapshot,
                package,
                book,
                policy=policy,
                status=IntegratedDecisionStatus.BLOCKED,
                reasons=("RISK_GUARD_UNAVAILABLE",),
                risk_result="REJECTED",
                risk_reason="RISK_GUARD_UNAVAILABLE",
                rules=(),
                candidate_fields=True,
            )
        if _limit_tuple(guard.config) != self._limits:
            raise GrowSafetyError("Risk Guard limits changed at runtime")
        proposal = _proposal(package, policy, snapshot)
        brief = _brief(self.config, snapshot, policy)
        try:
            verdict = guard.evaluate(
                proposal,
                brief,
                cash=book.cash,
                gross_notional=book.gross_notional,
                daily_pnl=book.daily_pnl,
                symbol_notional=book.symbol_notional,
                open_positions=book.open_positions,
            )
        except (GrowLiveTradingDisabled, GrowSafetyError, GrowConfigError) as exc:
            return self._finish(
                snapshot,
                package,
                book,
                policy=policy,
                status=IntegratedDecisionStatus.BLOCKED,
                reasons=("RISK_GUARD_REJECTED", type(exc).__name__),
                risk_result="REJECTED",
                risk_reason=str(exc),
                rules=(),
                candidate_fields=True,
            )
        rules = tuple(verdict.rule_results)
        if not verdict.approved or verdict.stamp is None:
            return self._finish(
                snapshot,
                package,
                book,
                policy=policy,
                status=IntegratedDecisionStatus.BLOCKED,
                reasons=("RISK_GUARD_REJECTED",),
                risk_result="REJECTED",
                risk_reason=verdict.reason,
                rules=rules,
                candidate_fields=True,
            )
        return self._finish(
            snapshot,
            package,
            book,
            policy=policy,
            status=IntegratedDecisionStatus.CANDIDATE,
            reasons=("PAPER_TRADE_CANDIDATE", "RISK_GUARD_APPROVED"),
            risk_result="APPROVED",
            risk_reason=verdict.reason,
            rules=rules,
            candidate_fields=True,
        )

    def _guard_for(self, as_of) -> RiskGuard | None:
        if self._risk_guard is not None:
            return self._risk_guard
        try:
            secret = self._risk_secret if self._risk_secret is not None else resolve_risk_secret()
        except GrowConfigError:
            return None
        return RiskGuard(self.config, clock=FrozenClock(as_of), secret=secret)

    def _finish(
        self,
        snapshot: AgentMarketSnapshot,
        package: AggregateAnalysisPackage,
        book: DecisionBookState,
        *,
        policy: PolicyResult,
        status: IntegratedDecisionStatus,
        reasons: tuple[str, ...],
        risk_result: str,
        risk_reason: str,
        rules: tuple[tuple[str, bool, str], ...],
        candidate_fields: bool = False,
    ) -> IntegratedDecision:
        candidate = policy.candidate if candidate_fields else None
        gates = policy.gates + (("risk_guard", risk_result == "APPROVED", risk_reason),)
        identity = {
            "schema": DECISION_SCHEMA,
            "analysis_cycle_id": package.cycle_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_version": snapshot.version,
            "as_of": snapshot.decision_timestamp.isoformat(),
            "status": status.value,
            "reason_codes": list(reasons),
            "candidate_strategy": None if candidate is None else candidate.strategy,
            "candidate_instrument": None if candidate is None else candidate.instrument,
            "direction": None if candidate is None else candidate.direction,
            "risk_guard_result": risk_result,
            "risk_guard_reason": risk_reason,
            "ruleset": self.config.risk.ruleset,
            "configuration_version": f"{self.config.version}:{self.config.risk.ruleset}",
            "package_digest": package.package_digest,
            "book": book.to_dict(),
            "data_quality_status": policy.data_quality,
            "agent_output_refs": [row.to_dict() for row in policy.refs],
            "gate_results": [{"gate": g, "passed": p, "detail": d} for g, p, d in gates],
            "conflicting_findings": list(policy.conflicting_findings),
        }
        decision_id = "dec-" + digest_payload(identity)
        audit_references = (
            f"package:{package.package_digest}",
            f"snapshot:{snapshot.snapshot_id}@{snapshot.version}",
            f"cycle:{package.cycle_id}",
            f"audit:{decision_id}",
        )
        return IntegratedDecision(
            decision_id=decision_id,
            analysis_cycle_id=package.cycle_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_version=snapshot.version,
            decision_timestamp=snapshot.decision_timestamp,
            as_of=snapshot.decision_timestamp,
            agent_output_refs=policy.refs,
            candidate_strategy=None if candidate is None else candidate.strategy,
            candidate_instrument=None if candidate is None else candidate.instrument,
            direction=None if candidate is None else candidate.direction,
            observations=policy.observations,
            calculated_evidence=policy.calculated_evidence,
            supporting_findings=policy.supporting_findings if candidate is not None else (),
            conflicting_findings=policy.conflicting_findings,
            status=status,
            reason_codes=tuple(dict.fromkeys(reasons)),
            risk_guard_result=risk_result,
            risk_guard_reason=risk_reason,
            risk_rule_results=rules,
            data_quality_status=policy.data_quality,
            assumptions=policy.assumptions,
            configuration_version=f"{self.config.version}:{self.config.risk.ruleset}",
            ruleset=self.config.risk.ruleset,
            audit_references=audit_references,
            gate_results=gates,
        )


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
        config.execution.mode,
        config.execution.live_trading_enabled,
    )


def _bind_failure(
    snapshot: AgentMarketSnapshot,
    package: AggregateAnalysisPackage,
    reason: str,
    _status: str,
) -> PolicyResult:
    return PolicyResult(
        terminal_status=_status,
        reason_codes=(reason,),
        candidate=None,
        refs=(),
        observations=tuple(snapshot.quality_notes),
        calculated_evidence={},
        supporting_findings=(),
        conflicting_findings=tuple(package.conflicts),
        assumptions=(
            "Risk Guard limits are not modified by agent outputs.",
            "CANDIDATE is a paper-trade candidate and is not a live or broker order.",
        ),
        gates=((reason.lower(), False, reason),),
        data_quality=snapshot.data_quality.value,
        regime_high_volatility=False,
    )


def _proposal(
    package: AggregateAnalysisPackage,
    policy: PolicyResult,
    snapshot: AgentMarketSnapshot,
) -> TradeProposal:
    candidate = policy.candidate
    if candidate is None:
        raise GrowSafetyError("missing candidate")
    notional = round(candidate.quantity * candidate.limit_price, 2)
    proposal_id = "prop-" + digest_payload(
        {
            "cycle": package.cycle_id,
            "instrument": candidate.instrument,
            "strategy": candidate.strategy,
            "qty": candidate.quantity,
            "limit": candidate.limit_price,
            "stop": candidate.stop_loss,
        }
    )
    return TradeProposal(
        proposal_id=proposal_id,
        symbol=Symbol(candidate.underlying),
        side=Side.BUY,
        intent=Intent.OPEN,
        quantity=candidate.quantity,
        limit_price=candidate.limit_price,
        stop_loss=candidate.stop_loss,
        take_profit=None,
        thesis=f"4C paper candidate {candidate.strategy} {candidate.instrument}",
        confidence=candidate.confidence,
        venue=Venue.PAPER,
        created_at=snapshot.decision_timestamp,
        notional=notional,
        extras={
            "analysis_cycle_id": package.cycle_id,
            "snapshot_id": snapshot.snapshot_id,
            "strategy": candidate.strategy,
            "direction": candidate.direction,
            "instrument": candidate.instrument,
            "executed": False,
        },
    )


def _brief(config: GrowConfig, snapshot: AgentMarketSnapshot, policy: PolicyResult) -> MarketBrief:
    candidate = policy.candidate
    if candidate is None:
        raise GrowSafetyError("missing candidate")
    calendar = SessionCalendar(config.market, clock=FrozenClock(snapshot.decision_timestamp))
    regime = Regime.HIGH_VOLATILITY if policy.regime_high_volatility else Regime.UNKNOWN
    return MarketBrief(
        symbol=Symbol(candidate.underlying, exchange=config.market.exchange),
        as_of=snapshot.decision_timestamp,
        session=calendar.state(snapshot.decision_timestamp),
        last_price=candidate.limit_price,
        currency=config.market.currency,
        regime=regime,
        notes=("4C decision brief from snapshot evidence",),
        source="grow.decision.integration",
        extras={"snapshot_id": snapshot.snapshot_id},
    )
