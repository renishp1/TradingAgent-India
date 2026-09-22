"""Fixture Research Director. Plans and reviews. Does not trade."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from grow.config import GrowConfig, load_config
from grow.director.capabilities import (
    DEFAULT_ACCEPTANCE,
    DEFAULT_CANDIDATE,
    DEFAULT_EXCLUSIONS,
    DEFAULT_METRICS,
    DEFAULT_STRESS,
)
from grow.director.catalog import FIXTURE_DATASET, default_catalog
from grow.director.models import (
    ACCEPT_FOR_PAPER,
    DRAFT,
    FROZEN,
    HOLD,
    READY,
    REFINE,
    REJECT,
    DirectorAudit,
    ResearchPlan,
    ResearchResult,
    ResearchReview,
    WindowSpec,
)
from grow.director.validate import ResearchPlanValidator
from grow.errors import GrowConfigError

DIRECTOR_VERSION = "research.director.fixture.v1"
PROMPT_VERSION = "director.plan.v1"


def _windows(start: date, end: date, embargo: int) -> tuple[WindowSpec, WindowSpec, WindowSpec]:
    span = (end - start).days
    if span < 12:
        raise GrowConfigError("INSUFFICIENT_COVERAGE")
    train_end = start + timedelta(days=max(span // 2, 4))
    val_start = train_end + timedelta(days=embargo)
    val_end = val_start + timedelta(days=max(span // 6, 2))
    test_start = val_end + timedelta(days=embargo)
    if test_start > end:
        raise GrowConfigError("INSUFFICIENT_COVERAGE")
    return WindowSpec(start, train_end), WindowSpec(val_start, val_end), WindowSpec(test_start, end)


def _plan_id(plan: ResearchPlan) -> str:
    return plan.fingerprint()[:16]


class FixtureDirector:
    def __init__(self, config: GrowConfig | None = None) -> None:
        self.config = config or load_config()
        rd = self.config.research_director
        if rd.provider != "fixture":
            raise GrowConfigError("2F research_director.provider must be fixture.")
        if rd.allow_broker or rd.allow_live_trading or rd.allow_paper_execution:
            raise GrowConfigError("2F director may not enable broker, live, or paper execution.")
        if rd.allow_ledger_write or rd.allow_risk_config_write:
            raise GrowConfigError("2F director may not write ledger or Risk Guard.")
        self.catalog = default_catalog()
        self.validator = ResearchPlanValidator()
        self.audit: list[DirectorAudit] = []
        self.plans: dict[str, ResearchPlan] = {}
        self.reviews: dict[str, ResearchReview] = {}
        self._test_seen: set[str] = set()

    def plan(
        self,
        *,
        question: str,
        hypothesis: str,
        dataset_id: str = FIXTURE_DATASET,
        universe: tuple[str, ...] = ("NIFTY",),
        start: date,
        end: date,
        granularity: str = "M15",
        parent_plan_id: str | None = None,
    ) -> ResearchPlan:
        embargo = self.config.backtest.embargo_sessions
        calendar = self.catalog[dataset_id].session_calendar_version if dataset_id in self.catalog else "unknown"
        try:
            train, validate, test = _windows(start, end, embargo)
            status = READY
        except GrowConfigError:
            train = WindowSpec(start, start)
            validate = WindowSpec(start, start)
            test = WindowSpec(end, end)
            status = DRAFT
        draft = ResearchPlan(
            plan_id="pending",
            parent_plan_id=parent_plan_id,
            research_question=question,
            hypothesis=hypothesis,
            universe=universe,
            dataset_id=dataset_id,
            dataset_version=dataset_id,
            historical_period=WindowSpec(start, end),
            data_granularity=granularity,
            session_calendar_version=calendar,
            train_window=train,
            validation_window=validate,
            test_window=test,
            embargo_gap=embargo,
            candidate_configurations=(DEFAULT_CANDIDATE,),
            selected_baseline_config=DEFAULT_CANDIDATE.config_id,
            stress_scenarios=DEFAULT_STRESS,
            metrics=DEFAULT_METRICS,
            acceptance_rules=DEFAULT_ACCEPTANCE,
            exclusion_rules=DEFAULT_EXCLUSIONS,
            calibration_mode="NONE",
            test_freeze_at=None,
            owner_role="CEO_RESEARCH_DIRECTOR",
            plan_schema_version="research.plan.v1",
            created_at=start.isoformat(),
            status=status,
            freeze_hash=None,
            prompt_version=PROMPT_VERSION,
            provider="fixture",
            model_name=DIRECTOR_VERSION,
        )
        plan = replace(draft, plan_id=_plan_id(draft))
        self.plans[plan.plan_id] = plan
        self._audit(plan, "PLAN", plan.status)
        return plan

    def freeze(self, plan: ResearchPlan) -> ResearchPlan:
        if plan.status == FROZEN and plan.freeze_hash:
            raise GrowConfigError("FROZEN_PLAN_IMMUTABLE")
        validated = self.validator.validate(plan, self.catalog)
        frozen = replace(
            validated.plan,
            status=FROZEN,
            test_freeze_at=validated.plan.historical_period.start.isoformat(),
            freeze_hash=validated.plan.fingerprint(),
        )
        if frozen.plan_id != plan.plan_id:
            raise GrowConfigError("PLAN_ID_DRIFT")
        self.plans[frozen.plan_id] = frozen
        self._audit(frozen, "FREEZE", FROZEN)
        return frozen

    def review(self, plan: ResearchPlan, result: ResearchResult) -> ResearchReview:
        if plan.status != FROZEN:
            raise GrowConfigError("TEST_BLIND_UNTIL_FROZEN")
        if result.plan_id != plan.plan_id or result.freeze_hash != plan.freeze_hash:
            raise GrowConfigError("RESULT_PLAN_MISMATCH")
        if not result.visible:
            raise GrowConfigError("TEST_BLIND_UNTIL_FROZEN")
        self._test_seen.add(plan.plan_id)
        failures: list[str] = []
        status = HOLD
        rationale = "Fixture dataset is not historical PIT validation. HOLD pending approved historical data."
        if result.leakage_status != "CLEAN":
            status = REJECT
            failures.append("LEAKAGE")
            rationale = "Leakage flag present. REJECT."
        elif not result.coverage_complete:
            status = REJECT
            failures.append("INCOMPLETE_COVERAGE")
            rationale = "Incomplete test coverage. REJECT."
        elif plan.dataset_id != FIXTURE_DATASET:
            status = HOLD
            rationale = "Non-fixture review is HOLD until a licensed PIT dataset is approved."
        if status == ACCEPT_FOR_PAPER:
            status = HOLD
            rationale = "ACCEPT_FOR_PAPER is forbidden on fixture data."
        review = ResearchReview(
            plan_id=plan.plan_id,
            result_id=result.result_id,
            observed_metrics=result.metrics,
            walk_forward_summary=result.walk_forward,
            stress_summary=result.stress_summary,
            data_quality_summary=result.data_quality,
            leakage_status=result.leakage_status,
            sample_size=result.sample_size,
            notable_failures=tuple(failures),
            ceo_assessment="facts=metrics; interpretation=fixture-only framework",
            status=status,
            rationale=rationale,
            next_research_question="Acquire an approved point-in-time historical option-chain dataset.",
            reviewer_version=DIRECTOR_VERSION,
            created_at=plan.created_at,
        )
        self.reviews[plan.plan_id] = review
        self._audit(plan, "REVIEW", status)
        return review

    def next_plan(self, review: ResearchReview) -> ResearchPlan:
        parent = self.plans[review.plan_id]
        child = self.plan(
            question=review.next_research_question,
            hypothesis=parent.hypothesis + " | follow-up after " + review.status,
            dataset_id=parent.dataset_id,
            universe=parent.universe,
            start=parent.historical_period.start,
            end=parent.historical_period.end,
            granularity=parent.data_granularity,
            parent_plan_id=parent.plan_id,
        )
        if child.plan_id == parent.plan_id:
            raise GrowConfigError("PLAN_ID_MUST_CHANGE")
        return child

    def mutate_frozen(self, plan: ResearchPlan, **changes) -> ResearchPlan:
        if plan.status == FROZEN:
            updated = replace(plan, status=READY, freeze_hash=None, test_freeze_at=None, **changes)
            rebuilt = replace(updated, plan_id=_plan_id(replace(updated, plan_id="pending")))
            if rebuilt.plan_id == plan.plan_id and changes:
                rebuilt = replace(rebuilt, plan_id=plan.plan_id + "-rev")
            return rebuilt
        raise GrowConfigError("NOT_FROZEN")

    def _audit(self, plan: ResearchPlan, decision: str, rationale: str) -> None:
        self.audit.append(
            DirectorAudit(
                audit_id=f"{plan.plan_id}:{decision}:{len(self.audit)}",
                plan_id=plan.plan_id,
                parent_plan_id=plan.parent_plan_id,
                actor_type="fixture_director",
                actor_version=DIRECTOR_VERSION,
                provider="fixture",
                prompt_version=plan.prompt_version,
                decision=decision,
                rationale=rationale,
                freeze_hash=plan.freeze_hash,
                created_at=plan.created_at,
            )
        )
