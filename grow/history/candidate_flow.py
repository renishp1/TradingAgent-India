"""Versioned multi-underlying 2C candidate set. Does not change 2D defaults.

CEO may approve an existing candidate or return NO_TRADE.
CEO may not invent underlying, expiry, strike, or option type.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Mapping

from grow.config import load_config
from grow.history.bridge import HistoricalMarketSource, HistoricalOptionSource
from grow.history.expiry import universe_at
from grow.history.resolver import resolve_nearest_expiry
from grow.history.store import CanonicalStore
from grow.history.universe import default_index_registry, discover_underlyings
from grow.options.engine import IndexOptionsEngine
from grow.options.models import OptionsDecision
from grow.strategies.signal import StrategySignal

FLOW_VERSION = "dynamic.candidate.v1"


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

    def to_dict(self) -> dict[str, object]:
        cand = None if self.decision.candidate is None else self.decision.candidate.to_dict()
        return {
            "underlying": self.underlying,
            "as_of": self.as_of,
            "direction": self.direction,
            "strategy_signal_id": self.strategy_signal_id,
            "policy_profile": self.policy_profile,
            "selected_expiry": self.selected_expiry,
            "status": self.decision.status.value,
            "candidate": cand,
            "diagnostics": list(self.decision.diagnostics),
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_fingerprint": self.dataset_fingerprint,
            "flow_version": FLOW_VERSION,
        }


@dataclass(frozen=True)
class CandidateSet:
    as_of: str
    contexts: tuple[UnderlyingDecisionContext, ...]
    flow_version: str = FLOW_VERSION

    def candidates(self) -> tuple[UnderlyingDecisionContext, ...]:
        return tuple(row for row in self.contexts if row.decision.candidate is not None)


class DynamicCandidateOrchestrator:
    """One 2C candidate per eligible underlying. Not a 2D behavior change."""

    def __init__(self, store: CanonicalStore) -> None:
        self.store = store
        self.market = HistoricalMarketSource(store)
        self.options = HistoricalOptionSource(store)
        self.engine = IndexOptionsEngine(load_config())
        self.registry = default_index_registry()

    def evaluate(self, as_of: datetime, signals: Mapping[str, StrategySignal]) -> CandidateSet:
        discovered = discover_underlyings(self.store, as_of, self.registry)
        rows: list[UnderlyingDecisionContext] = []
        meta = self.store.meta
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
            resolved = resolve_nearest_expiry(
                item.canonical_symbol,
                as_of,
                visible,
                item.expiry_policy_profile,
                dataset_id=meta.dataset_id,
                dataset_version=meta.version,
                dataset_fingerprint=meta.fingerprint,
            )
            decision = self.engine.evaluate(aligned, snap, chain)
            rows.append(
                UnderlyingDecisionContext(
                    underlying=item.canonical_symbol,
                    as_of=as_of.isoformat(),
                    direction=signal.direction,
                    strategy_signal_id=signal.signal_id or signal.snapshot_id,
                    policy_profile=item.expiry_policy_profile,
                    selected_expiry=resolved.selected_expiry,
                    decision=decision,
                    dataset_id=meta.dataset_id,
                    dataset_version=meta.version,
                    dataset_fingerprint=meta.fingerprint,
                )
            )
        return CandidateSet(as_of=as_of.isoformat(), contexts=tuple(rows))
