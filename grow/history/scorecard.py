"""Factual provider scorecard. No ranking. Not a profitability claim."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from grow.history.eval import ProviderEvaluationResult
from grow.history.store import CanonicalStore
from grow.history.universe import default_index_registry, discover_underlyings


@dataclass(frozen=True)
class ProviderScorecard:
    provider_name: str
    dataset_id: str
    dataset_version: str
    fingerprint: str
    qualification_state: str
    coverage_start: str
    coverage_end: str
    supported_indices: tuple[str, ...]
    expiry_classes: tuple[tuple[str, str], ...]
    granularity: tuple[str, ...]
    bid_ask_coverage: float
    oi_coverage: float
    volume_coverage: float
    iv_coverage: float
    greeks_coverage: float
    lot_size: str
    pit_status: str
    limitations: tuple[str, ...]
    live: bool = False
    label: str = "HISTORICAL RESEARCH / NOT LIVE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "fingerprint": self.fingerprint,
            "qualification_state": self.qualification_state,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "supported_indices": list(self.supported_indices),
            "expiry_classes": {k: v for k, v in self.expiry_classes},
            "granularity": list(self.granularity),
            "bid_ask_coverage": self.bid_ask_coverage,
            "oi_coverage": self.oi_coverage,
            "volume_coverage": self.volume_coverage,
            "iv_coverage": self.iv_coverage,
            "greeks_coverage": self.greeks_coverage,
            "lot_size": self.lot_size,
            "pit_status": self.pit_status,
            "limitations": list(self.limitations),
            "live": self.live,
            "label": self.label,
            "ranking": None,
            "profitability_claim": False,
        }


def scorecard_from(store: CanonicalStore, result: ProviderEvaluationResult) -> ProviderScorecard:
    report = store.coverage()
    from datetime import datetime, time

    from grow.clock import IST

    days = [s.session_date for s in store._sessions.values() if s.status == "OPEN"]
    as_of = datetime.combine(days[0], time(11, 0), tzinfo=IST) if days else datetime(2026, 9, 7, 11, 0, tzinfo=IST)
    discovered = discover_underlyings(store, as_of)
    eligible = tuple(row.canonical_symbol for row in discovered if row.status == "ELIGIBLE")
    classes: list[tuple[str, str]] = []
    registry = default_index_registry()
    for symbol in eligible:
        pol = registry.policy(symbol)
        classes.append((symbol, pol.expiry_policy_profile if pol else "UNKNOWN"))
    pit = next((c.outcome for c in result.checks if c.name == "PIT"), "UNKNOWN")
    lots = "present" if all(c.lot_size for c in store.all_contracts()) else "missing"
    return ProviderScorecard(
        provider_name=store.meta.provider_name,
        dataset_id=store.meta.dataset_id,
        dataset_version=store.meta.version,
        fingerprint=store.meta.fingerprint,
        qualification_state=result.qualification_status,
        coverage_start=store.meta.coverage_start.isoformat(),
        coverage_end=store.meta.coverage_end.isoformat(),
        supported_indices=eligible,
        expiry_classes=tuple(classes),
        granularity=store.meta.granularity,
        bid_ask_coverage=report.bid_ask_completeness,
        oi_coverage=report.oi_completeness,
        volume_coverage=report.volume_completeness,
        iv_coverage=report.iv_completeness,
        greeks_coverage=0.0 if not store.meta.greeks_available else 1.0,
        lot_size=lots,
        pit_status=pit,
        limitations=result.limitations,
    )
