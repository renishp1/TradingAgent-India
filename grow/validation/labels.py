"""Evaluation data-class labels for validation campaigns.

Distinct from MarketDataSource (LIVE / FIXTURE / MIXED). A walk-forward or
paper campaign must carry exactly one of:

  FIXTURE | SYNTHETIC | HISTORICAL | LIVE-PAPER

Mixing labels in one evaluation is forbidden. Profitability must never be
claimed from FIXTURE or SYNTHETIC data.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Mapping

from grow.errors import GrowConfigError
from grow.history.models import (
    ADAPTER_TESTING,
    APPROVED_FOR_2E,
    FRAMEWORK_TEST_ONLY,
    HISTORICAL_RESEARCH,
    QUALIFIED,
    SYNTHETIC,
    is_recorded_integration_sample,
)
from grow.history.store import CanonicalStore


class EvaluationLabel(str, Enum):
    FIXTURE = "FIXTURE"
    SYNTHETIC = "SYNTHETIC"
    HISTORICAL = "HISTORICAL"
    LIVE_PAPER = "LIVE-PAPER"


KNOWN_EVALUATION_LABELS = frozenset(item.value for item in EvaluationLabel)
HISTORICAL_READY = frozenset({QUALIFIED, APPROVED_FOR_2E})


def parse_evaluation_label(value: str | EvaluationLabel) -> EvaluationLabel:
    text = value.value if isinstance(value, EvaluationLabel) else str(value).strip().upper()
    # Accept both LIVE_PAPER and LIVE-PAPER spellings.
    if text in {"LIVE_PAPER", "LIVE-PAPER"}:
        return EvaluationLabel.LIVE_PAPER
    try:
        return EvaluationLabel(text)
    except ValueError as exc:
        raise GrowConfigError(f"UNKNOWN_EVALUATION_LABEL:{text}") from exc


def assert_single_evaluation_label(labels: Iterable[str | EvaluationLabel]) -> EvaluationLabel:
    """Fail closed when more than one evaluation class appears in one run."""

    normalized = {parse_evaluation_label(item) for item in labels}
    if not normalized:
        raise GrowConfigError("EVALUATION_LABEL_REQUIRED")
    if len(normalized) > 1:
        joined = ",".join(sorted(item.value for item in normalized))
        raise GrowConfigError(f"EVALUATION_LABEL_MIXED:{joined}")
    return next(iter(normalized))


def assert_no_fixture_profitability_claim(
    label: str | EvaluationLabel,
    *,
    profitability_claim: bool,
) -> None:
    """Never allow profitability claims from FIXTURE or SYNTHETIC evaluations."""

    resolved = parse_evaluation_label(label)
    if profitability_claim and resolved in {EvaluationLabel.FIXTURE, EvaluationLabel.SYNTHETIC}:
        raise GrowConfigError("FIXTURE_PROFITABILITY_CLAIM_FORBIDDEN")
    if profitability_claim:
        # Descriptive research metrics only — no profitability claim for any label.
        raise GrowConfigError("PROFITABILITY_CLAIM_FORBIDDEN")


def classify_store_evaluation_label(store: CanonicalStore) -> EvaluationLabel:
    """Derive the evaluation label from a canonical historical store."""

    meta = store.meta
    if meta.is_fixture or meta.usage_scope == FRAMEWORK_TEST_ONLY:
        if meta.quality_status == SYNTHETIC or "SYNTHETIC" in (meta.provenance or "").upper():
            return EvaluationLabel.SYNTHETIC
        return EvaluationLabel.FIXTURE
    if meta.usage_scope == ADAPTER_TESTING or is_recorded_integration_sample(meta):
        return EvaluationLabel.SYNTHETIC
    if meta.quality_status == SYNTHETIC:
        return EvaluationLabel.SYNTHETIC
    if meta.usage_scope == HISTORICAL_RESEARCH and not meta.is_fixture:
        return EvaluationLabel.HISTORICAL
    raise GrowConfigError(f"EVALUATION_LABEL_UNCLASSIFIED:{meta.usage_scope}")


def require_historical_evaluation_dataset(
    store: CanonicalStore,
    qualification: Mapping[str, Any] | None = None,
) -> None:
    """HISTORICAL walk-forward requires licensed, non-fixture PIT research data."""

    meta = store.meta
    if meta.is_fixture:
        raise GrowConfigError("FIXTURE_AS_HISTORICAL_FORBIDDEN")
    if meta.usage_scope == FRAMEWORK_TEST_ONLY:
        raise GrowConfigError("FIXTURE_AS_HISTORICAL_FORBIDDEN")
    if meta.usage_scope == ADAPTER_TESTING or is_recorded_integration_sample(meta):
        raise GrowConfigError("SYNTHETIC_AS_HISTORICAL_FORBIDDEN")
    if meta.quality_status == SYNTHETIC:
        raise GrowConfigError("SYNTHETIC_AS_HISTORICAL_FORBIDDEN")
    if meta.usage_scope != HISTORICAL_RESEARCH:
        raise GrowConfigError(f"HISTORICAL_USAGE_SCOPE_REQUIRED:{meta.usage_scope}")
    if not meta.fingerprint or meta.fingerprint == "pending":
        raise GrowConfigError("HISTORICAL_DATASET_UNPUBLISHED")
    if not meta.contract_metadata_available:
        raise GrowConfigError("HISTORICAL_CONTRACT_MASTER_REQUIRED")
    status = None
    if qualification is not None:
        status = qualification.get("qualification_status")
        if qualification.get("dataset_id") != meta.dataset_id:
            raise GrowConfigError("QUALIFICATION_DATASET_MISMATCH")
        if qualification.get("dataset_version") != meta.version:
            raise GrowConfigError("QUALIFICATION_VERSION_MISMATCH")
        if qualification.get("fingerprint") != meta.fingerprint:
            raise GrowConfigError("QUALIFICATION_FINGERPRINT_MISMATCH")
    else:
        status = meta.qualification_status
    if status not in HISTORICAL_READY:
        raise GrowConfigError(f"HISTORICAL_DATASET_NOT_READY:{status}")


def label_payload(label: str | EvaluationLabel) -> dict[str, Any]:
    resolved = parse_evaluation_label(label)
    return {
        "evaluation_label": resolved.value,
        "profitability_claim": False,
        "live": False,
        "broker_order_path": False,
        "label": f"{resolved.value} VALIDATION / NOT LIVE",
    }
