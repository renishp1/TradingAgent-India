"""Deterministic ResearchPlan validator. Fail closed."""

from __future__ import annotations

from dataclasses import dataclass

from grow.director.capabilities import HARD_LOCKS, candidate_by_id
from grow.director.catalog import APPROVED, APPROVED_WITH_WARNINGS, ApprovedDataSource, acknowledge_dataset_warnings
from grow.director.models import FROZEN, READY, ResearchPlan
from grow.errors import GrowConfigError

FORBIDDEN_SAFETY = (
    "allow_same_day",
    "allow_short",
    "live_trading",
    "broker",
    "sell_option",
    "calibrate_on_test",
)


@dataclass(frozen=True)
class ValidatedResearchPlan:
    plan: ResearchPlan
    catalog_ref: str
    issues: tuple[str, ...]


class ResearchPlanValidator:
    def validate(
        self,
        plan: ResearchPlan,
        catalog: dict[str, ApprovedDataSource],
    ) -> ValidatedResearchPlan:
        issues: list[str] = []
        if not plan.research_question.strip() or not plan.hypothesis.strip():
            issues.append("VAGUE_RESEARCH_OBJECTIVE")
        if not plan.acceptance_rules:
            issues.append("MISSING_ACCEPTANCE_RULES")
        if plan.calibration_mode != "NONE":
            issues.append("TEST_WINDOW_TUNING")
        source = catalog.get(plan.dataset_id)
        if source is None:
            issues.append(f"UNKNOWN_DATASET:{plan.dataset_id}")
        else:
            if source.licensing_status != APPROVED:
                issues.append(f"DATASET_NOT_APPROVED:{plan.dataset_id}")
            if source.usage_scope == "HISTORICAL_RESEARCH":
                if source.quality_status == APPROVED_WITH_WARNINGS:
                    if not source.quality_warnings:
                        issues.append("WARNINGS_NOT_ACKNOWLEDGED")
                    issues.extend(
                        acknowledge_dataset_warnings(source.quality_warnings, plan.accepted_dataset_warnings)
                    )
                elif source.quality_status != APPROVED:
                    issues.append(f"DATASET_NOT_APPROVED:{plan.dataset_id}")
                else:
                    issues.extend(
                        acknowledge_dataset_warnings(source.quality_warnings, plan.accepted_dataset_warnings)
                    )
            start, end = plan.historical_period.start, plan.historical_period.end
            if start < source.date_coverage[0] or end > source.date_coverage[1] or start > end:
                issues.append("INSUFFICIENT_COVERAGE")
            if plan.data_granularity not in source.timestamp_granularity:
                issues.append(f"UNSUPPORTED_GRANULARITY:{plan.data_granularity}")
            if plan.data_granularity != "M15":
                issues.append("GRANULARITY_NOT_2E_M15")
            if plan.session_calendar_version != source.session_calendar_version:
                issues.append("CALENDAR_MISMATCH")
            for ticker in plan.universe:
                if ticker not in source.instrument_scope:
                    issues.append(f"UNIVERSE:{ticker}")
        if plan.train_window.end >= plan.validation_window.start:
            issues.append("WINDOW_ORDER_TRAIN_VALIDATE")
        if plan.validation_window.end >= plan.test_window.start:
            issues.append("WINDOW_ORDER_VALIDATE_TEST")
        if plan.embargo_gap < 1:
            issues.append("EMBARGO")
        if plan.selected_baseline_config != (plan.candidate_configurations[0].config_id if plan.candidate_configurations else ""):
            issues.append("UNKNOWN_CANDIDATE")
        for cand in plan.candidate_configurations:
            if candidate_by_id(cand.config_id) is None:
                issues.append(f"UNDECLARED_CONFIG:{cand.config_id}")
        blob = " ".join(plan.to_dict().keys()) + plan.research_question + plan.hypothesis + "".join(plan.exclusion_rules)
        for token in FORBIDDEN_SAFETY:
            if token in blob.lower() and token in (plan.research_question + plan.hypothesis).lower():
                issues.append(f"SAFETY_MUTATION:{token}")
        for lock in HARD_LOCKS:
            if lock in plan.exclusion_rules and lock.startswith("no_"):
                continue
        if plan.status not in {READY, FROZEN}:
            issues.append(f"NOT_READY:{plan.status}")
        if issues:
            raise GrowConfigError(";".join(issues))
        return ValidatedResearchPlan(plan=plan, catalog_ref=plan.dataset_id, issues=())
