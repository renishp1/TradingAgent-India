"""Versioned multi-underlying 2C candidate set. Does not change 2D defaults.

CEO may approve an existing candidate or return NO_TRADE.
CEO may not invent underlying, expiry, strike, or option type.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping

from grow.config import load_config
from grow.errors import GrowConfigError
from grow.history.bridge import HistoricalMarketSource, HistoricalOptionSource
from grow.history.eval import QUALIFICATION_SCHEMA, DatasetQualificationRecord
from grow.history.expiry import universe_at
from grow.history.models import APPROVED_FOR_2E, FRAMEWORK_TEST_ONLY
from grow.history.resolver import ExpiryResolution, resolve_nearest_expiry
from grow.history.store import CanonicalStore
from grow.history.universe import default_index_registry, discover_underlyings
from grow.options.engine import IndexOptionsEngine
from grow.options.models import DecisionStatus, OptionsDecision
from grow.research.models import freeze_map
from grow.strategies.signal import StrategySignal

FLOW_VERSION = "dynamic.candidate.v1"
CANDIDATE_SET_SCHEMA = "research.candidateset.v1"
EXPIRY_RESOLUTION_MISMATCH = "EXPIRY_RESOLUTION_MISMATCH"
MISSING_LOT_SIZE = "MISSING_LOT_SIZE"


def require_historical_qualification(store: CanonicalStore, record: DatasetQualificationRecord | None) -> None:
    meta = store.meta
    if meta.is_fixture:
        if record is not None and (record.approved_for_2e or record.qualification_status == APPROVED_FOR_2E):
            raise GrowConfigError("FRAMEWORK_TEST_ONLY cannot become APPROVED_FOR_2E")
        return
    if meta.usage_scope == FRAMEWORK_TEST_ONLY:
        raise GrowConfigError("FRAMEWORK_TEST_ONLY cannot enter historical 2C")
    if record is None:
        raise GrowConfigError("QUALIFICATION_REQUIRED")
    if record.schema != QUALIFICATION_SCHEMA:
        raise GrowConfigError("QUALIFICATION_SCHEMA")
    if record.dataset_id != meta.dataset_id:
        raise GrowConfigError("QUALIFICATION_DATASET_MISMATCH")
    if record.dataset_version != meta.version:
        raise GrowConfigError("QUALIFICATION_VERSION_MISMATCH")
    if record.fingerprint != meta.fingerprint:
        raise GrowConfigError("QUALIFICATION_FINGERPRINT_MISMATCH")
    if record.qualification_status != APPROVED_FOR_2E or not record.approved_for_2e:
        raise GrowConfigError("HISTORICAL_NOT_APPROVED_FOR_2E")
    if record.approved_for_2e != (record.qualification_status == APPROVED_FOR_2E):
        raise GrowConfigError("QUALIFICATION_APPROVAL_INCONSISTENT")


@dataclass(frozen=True)
class UnderlyingDecisionContext:
    underlying: str
    as_of: str
    direction: str
    strategy_signal_id: str
    policy_profile: str
    selected_expiry: str | None
    decision: OptionsDecision
    dataset_id: str
    dataset_version: str
    dataset_fingerprint: str
    resolution_id: str = ""
    policy_registry_version: str = ""
    policy_fingerprint: str = ""
    strike_policy_profile: str = ""
    liquidity_policy: str = ""
    lot_size_source: str = ""

    def to_dict(self) -> dict[str, object]:
        cand = None if self.decision.candidate is None else self.decision.candidate.to_dict()
        return {
            "underlying": self.underlying,
            "as_of": self.as_of,
            "direction": self.direction,
            "strategy_signal_id": self.strategy_signal_id,
            "policy_profile": self.policy_profile,
            "strike_policy_profile": self.strike_policy_profile,
            "liquidity_policy": self.liquidity_policy,
            "lot_size_source": self.lot_size_source,
            "selected_expiry": self.selected_expiry,
            "resolution_id": self.resolution_id,
            "policy_registry_version": self.policy_registry_version,
            "policy_fingerprint": self.policy_fingerprint,
            "status": self.decision.status.value,
            "candidate": cand,
            "diagnostics": list(self.decision.diagnostics),
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_fingerprint": self.dataset_fingerprint,
            "flow_version": FLOW_VERSION,
        }

    def evidence(self) -> Mapping[str, Any]:
        cand = {} if self.decision.candidate is None else self.decision.candidate.to_dict()
        payload = {
            **cand,
            "candidate_id": cand.get("candidate_id"),
            "underlying": cand.get("underlying", self.underlying),
            "direction": cand.get("direction", self.direction),
            "option_type": cand.get("option_type"),
            "expiry": cand.get("expiry", self.selected_expiry),
            "strike": cand.get("strike"),
            "contract_symbol": cand.get("contract_symbol"),
            "provider_contract_id": cand.get("contract_symbol"),
            "spot_price": cand.get("spot_price"),
            "premium_reference": cand.get("premium_reference"),
            "bid": cand.get("bid"),
            "ask": cand.get("ask"),
            "spread": cand.get("spread"),
            "volume": cand.get("volume"),
            "open_interest": cand.get("open_interest"),
            "implied_volatility": cand.get("implied_volatility"),
            "delta": cand.get("delta"),
            "gamma": cand.get("gamma"),
            "theta": cand.get("theta"),
            "vega": cand.get("vega"),
            "intrinsic_value": cand.get("intrinsic_value"),
            "extrinsic_value": cand.get("extrinsic_value"),
            "score": cand.get("score"),
            "score_breakdown": cand.get("score"),
            "underlying_snapshot_id": cand.get("underlying_snapshot_id"),
            "option_chain_snapshot_id": cand.get("option_chain_snapshot_id"),
            "strategy_signal_id": cand.get("strategy_signal_id", self.strategy_signal_id),
            "strategy_version": cand.get("strategy_version"),
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_fingerprint": self.dataset_fingerprint,
            "resolution_id": self.resolution_id,
            "policy_registry_version": self.policy_registry_version,
            "policy_fingerprint": self.policy_fingerprint,
            "expiry_policy_profile": self.policy_profile,
            "strike_policy_profile": self.strike_policy_profile,
            "liquidity_policy": self.liquidity_policy,
            "lot_size_source": self.lot_size_source,
            "diagnostics": list(self.decision.diagnostics),
            "status": self.decision.status.value,
        }
        return freeze_map(payload)


@dataclass(frozen=True)
class CandidateSet:
    as_of: str
    contexts: tuple[UnderlyingDecisionContext, ...]
    flow_version: str = FLOW_VERSION
    schema: str = CANDIDATE_SET_SCHEMA
    policy_registry_version: str = ""
    policy_fingerprint: str = ""

    def candidates(self) -> tuple[UnderlyingDecisionContext, ...]:
        return tuple(row for row in self.contexts if row.decision.candidate is not None)

    def to_research_input(self) -> Mapping[str, Any]:
        payload = {
            "schema": self.schema,
            "flow_version": self.flow_version,
            "as_of": self.as_of,
            "policy_registry_version": self.policy_registry_version,
            "policy_fingerprint": self.policy_fingerprint,
            "candidates": [row.evidence() for row in self.candidates()],
            "audit": [row.to_dict() for row in self.contexts],
            "ceo_may": "select one existing candidate_id or NO_TRADE",
            "ceo_may_not": [
                "invent underlying",
                "change expiry",
                "change strike",
                "convert CE/PE",
                "place orders",
            ],
        }
        return freeze_map(payload)

    def pick(self, candidate_id: str | None) -> Mapping[str, Any]:
        if not candidate_id:
            return freeze_map({"verdict": "NO_TRADE", "reason": "NO_SELECTION", "candidate": None})
        matches = [row for row in self.candidates() if row.decision.candidate.candidate_id == candidate_id]
        if len(matches) != 1:
            return freeze_map({"verdict": "NO_TRADE", "reason": "UNKNOWN_CANDIDATE", "candidate": None})
        return freeze_map(
            {
                "verdict": "TRADE_APPROVE",
                "reason": "EXISTING_CANDIDATE",
                "candidate": matches[0].evidence(),
            }
        )


def bind_resolution(decision: OptionsDecision, resolved: ExpiryResolution) -> OptionsDecision:
    if decision.candidate is None:
        return decision
    selected = resolved.selected_expiry
    got = decision.candidate.expiry.isoformat()
    if selected is None or got != selected:
        return replace(
            decision,
            status=DecisionStatus.NO_TRADE,
            candidate=None,
            diagnostics=decision.diagnostics + (EXPIRY_RESOLUTION_MISMATCH,),
        )
    return decision


def _require_lot(store: CanonicalStore, decision: OptionsDecision, source: str) -> OptionsDecision:
    if decision.candidate is None or source != "CONTRACT_MASTER":
        return decision
    cid = decision.candidate.contract_symbol
    try:
        lot = store.lot_size(cid) if store.has_contract(cid) else None
    except GrowConfigError:
        lot = None
    if lot is None or lot < 1:
        return replace(
            decision,
            status=DecisionStatus.NO_TRADE,
            candidate=None,
            diagnostics=decision.diagnostics + (MISSING_LOT_SIZE,),
        )
    return decision


class DynamicCandidateOrchestrator:
    """One 2C candidate per eligible underlying. Not a 2D behavior change."""

    def __init__(
        self,
        store: CanonicalStore,
        registry=None,
        qualification: DatasetQualificationRecord | None = None,
    ) -> None:
        require_historical_qualification(store, qualification)
        self.store = store
        self.qualification = qualification
        self.market = HistoricalMarketSource(store)
        self.options = HistoricalOptionSource(store)
        cfg = load_config()
        if not store.meta.is_fixture:
            cfg = replace(cfg, options=replace(cfg.options, provider="historical"))
        self.engine = IndexOptionsEngine(cfg)
        self.registry = registry or default_index_registry()

    def evaluate(self, as_of: datetime, signals: Mapping[str, StrategySignal]) -> CandidateSet:
        discovered = discover_underlyings(self.store, as_of, self.registry)
        rows: list[UnderlyingDecisionContext] = []
        meta = self.store.meta
        policy_fp = self.registry.fingerprint
        policy_ver = self.registry.version
        for item in discovered:
            if item.status != "ELIGIBLE":
                continue
            signal = signals.get(item.canonical_symbol)
            if signal is None:
                continue
            if signal.symbol.ticker != item.canonical_symbol:
                continue
            snap = self.market.snapshot(item.canonical_symbol, as_of)
            aligned = replace(signal, snapshot_id=snap.snapshot_id, as_of=snap.as_of)
            chain = self.options.snapshot(item.canonical_symbol, as_of, spot=snap.last_price)
            visible = universe_at(self.store, item.canonical_symbol, as_of)
            index_policy = self.registry.policy(item.canonical_symbol, as_of.date())
            resolved = resolve_nearest_expiry(
                item.canonical_symbol,
                as_of,
                visible,
                item.expiry_policy_profile,
                dataset_id=meta.dataset_id,
                dataset_version=meta.version,
                dataset_fingerprint=meta.fingerprint,
                policy_registry_version=policy_ver,
                policy_fingerprint=policy_fp,
            )
            decision = bind_resolution(
                self.engine.evaluate(aligned, snap, chain, registry=self.registry),
                resolved,
            )
            if index_policy is not None:
                decision = _require_lot(self.store, decision, index_policy.lot_size_source)
            rows.append(
                UnderlyingDecisionContext(
                    underlying=item.canonical_symbol,
                    as_of=as_of.isoformat(),
                    direction=signal.direction,
                    strategy_signal_id=signal.signal_id or signal.snapshot_id,
                    policy_profile=item.expiry_policy_profile,
                    strike_policy_profile="" if index_policy is None else index_policy.strike_policy_profile,
                    liquidity_policy="" if index_policy is None else index_policy.liquidity_policy,
                    lot_size_source="" if index_policy is None else index_policy.lot_size_source,
                    selected_expiry=resolved.selected_expiry,
                    resolution_id=resolved.resolution_id,
                    policy_registry_version=policy_ver,
                    policy_fingerprint=policy_fp,
                    decision=decision,
                    dataset_id=meta.dataset_id,
                    dataset_version=meta.version,
                    dataset_fingerprint=meta.fingerprint,
                )
            )
        return CandidateSet(
            as_of=as_of.isoformat(),
            contexts=tuple(rows),
            policy_registry_version=policy_ver,
            policy_fingerprint=policy_fp,
        )
