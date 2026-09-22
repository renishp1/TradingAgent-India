"""2F research-governance contracts. Not trades. Not live."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

PLAN_SCHEMA = "research.plan.v1"
REVIEW_SCHEMA = "research.review.v1"
RESULT_SCHEMA = "research.result.v1"
AUDIT_SCHEMA = "research.director.audit.v1"
CONFIG_CANDIDATE = "grow.default.v1"

DRAFT = "DRAFT"
READY = "READY_FOR_VALIDATION"
FROZEN = "FROZEN"
RUNNING = "RUNNING"
REVIEW_PENDING = "REVIEW_PENDING"
ACCEPT_FOR_PAPER = "ACCEPT_FOR_PAPER"
REFINE = "REFINE"
HOLD = "HOLD"
REJECT = "REJECT"


def _digest(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WindowSpec:
    start: date
    end: date

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True)
class ConfigCandidate:
    config_id: str
    version: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {"config_id": self.config_id, "version": self.version, "description": self.description}


@dataclass(frozen=True)
class ResearchPlan:
    plan_id: str
    parent_plan_id: str | None
    research_question: str
    hypothesis: str
    universe: tuple[str, ...]
    dataset_id: str
    dataset_version: str
    historical_period: WindowSpec
    data_granularity: str
    session_calendar_version: str
    train_window: WindowSpec
    validation_window: WindowSpec
    test_window: WindowSpec
    embargo_gap: int
    candidate_configurations: tuple[ConfigCandidate, ...]
    selected_baseline_config: str
    stress_scenarios: tuple[str, ...]
    metrics: tuple[str, ...]
    acceptance_rules: tuple[str, ...]
    exclusion_rules: tuple[str, ...]
    calibration_mode: str
    test_freeze_at: str | None
    owner_role: str
    plan_schema_version: str
    created_at: str
    status: str
    freeze_hash: str | None
    prompt_version: str
    provider: str
    model_name: str

    def freeze_payload(self) -> dict[str, Any]:
        return {
            "research_question": self.research_question,
            "hypothesis": self.hypothesis,
            "universe": list(self.universe),
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "historical_period": self.historical_period.to_dict(),
            "data_granularity": self.data_granularity,
            "session_calendar_version": self.session_calendar_version,
            "train_window": self.train_window.to_dict(),
            "validation_window": self.validation_window.to_dict(),
            "test_window": self.test_window.to_dict(),
            "embargo_gap": self.embargo_gap,
            "candidate_configurations": [c.to_dict() for c in self.candidate_configurations],
            "selected_baseline_config": self.selected_baseline_config,
            "stress_scenarios": list(self.stress_scenarios),
            "metrics": list(self.metrics),
            "acceptance_rules": list(self.acceptance_rules),
            "exclusion_rules": list(self.exclusion_rules),
            "calibration_mode": self.calibration_mode,
            "plan_schema_version": self.plan_schema_version,
        }

    def fingerprint(self) -> str:
        return _digest(self.freeze_payload())

    def to_dict(self) -> dict[str, Any]:
        body = self.freeze_payload()
        body.update(
            {
                "plan_id": self.plan_id,
                "parent_plan_id": self.parent_plan_id,
                "test_freeze_at": self.test_freeze_at,
                "owner_role": self.owner_role,
                "created_at": self.created_at,
                "status": self.status,
                "freeze_hash": self.freeze_hash,
                "prompt_version": self.prompt_version,
                "provider": self.provider,
                "model_name": self.model_name,
                "live": False,
            }
        )
        return body


@dataclass(frozen=True)
class ResearchResult:
    result_id: str
    plan_id: str
    freeze_hash: str
    dataset_id: str
    dataset_version: str
    code_commit: str
    metrics: Mapping[str, Any]
    walk_forward: Mapping[str, Any]
    stress_summary: Mapping[str, Any]
    data_quality: Mapping[str, Any]
    leakage_status: str
    sample_size: int
    coverage_complete: bool
    visible: bool
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "plan_id": self.plan_id,
            "freeze_hash": self.freeze_hash,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "code_commit": self.code_commit,
            "metrics": dict(self.metrics),
            "walk_forward": dict(self.walk_forward),
            "stress_summary": dict(self.stress_summary),
            "data_quality": dict(self.data_quality),
            "leakage_status": self.leakage_status,
            "sample_size": self.sample_size,
            "coverage_complete": self.coverage_complete,
            "visible": self.visible,
            "created_at": self.created_at,
            "schema": RESULT_SCHEMA,
            "label": "RESEARCH GOVERNANCE / NOT LIVE",
        }


@dataclass(frozen=True)
class ResearchReview:
    plan_id: str
    result_id: str
    observed_metrics: Mapping[str, Any]
    walk_forward_summary: Mapping[str, Any]
    stress_summary: Mapping[str, Any]
    data_quality_summary: Mapping[str, Any]
    leakage_status: str
    sample_size: int
    notable_failures: tuple[str, ...]
    ceo_assessment: str
    status: str
    rationale: str
    next_research_question: str
    reviewer_version: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "result_id": self.result_id,
            "observed_metrics": dict(self.observed_metrics),
            "walk_forward_summary": dict(self.walk_forward_summary),
            "stress_summary": dict(self.stress_summary),
            "data_quality_summary": dict(self.data_quality_summary),
            "leakage_status": self.leakage_status,
            "sample_size": self.sample_size,
            "notable_failures": list(self.notable_failures),
            "ceo_assessment": self.ceo_assessment,
            "status": self.status,
            "rationale": self.rationale,
            "next_research_question": self.next_research_question,
            "reviewer_version": self.reviewer_version,
            "created_at": self.created_at,
            "schema": REVIEW_SCHEMA,
            "label": "RESEARCH GOVERNANCE / NOT LIVE",
        }


@dataclass(frozen=True)
class DirectorAudit:
    audit_id: str
    plan_id: str
    parent_plan_id: str | None
    actor_type: str
    actor_version: str
    provider: str
    prompt_version: str
    decision: str
    rationale: str
    freeze_hash: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "plan_id": self.plan_id,
            "parent_plan_id": self.parent_plan_id,
            "actor_type": self.actor_type,
            "actor_version": self.actor_version,
            "provider": self.provider,
            "prompt_version": self.prompt_version,
            "decision": self.decision,
            "rationale": self.rationale,
            "freeze_hash": self.freeze_hash,
            "created_at": self.created_at,
            "schema": AUDIT_SCHEMA,
        }
