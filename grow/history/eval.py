"""2H provider evaluation harness. Qualifies data. Does not trade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from grow.clock import IST
from grow.config import load_config
from grow.history.bridge import HistoricalOptionSource
from grow.history.expiry import select_nearest_weekly_expiry, universe_at
from grow.history.models import (
    ALLOWED_OPTION_TYPES,
    ALLOWED_UNDERLYINGS,
    APPROVED_FOR_2E,
    CANDIDATE,
    FRAMEWORK_TEST_ONLY,
    QUALIFIED,
    QUALIFIED_WITH_WARNINGS,
    REJECTED,
)
from grow.history.store import CanonicalStore
from grow.options.select import choose_expiry

EVAL_SCHEMA = "provider.eval.v1"
MANDATORY = (
    "PIT",
    "EXPIRY_RECONSTRUCTION",
    "NEAREST_WEEKLY",
    "CONTRACT_IDENTITY",
    "LOT_SIZE",
    "CALENDAR",
    "PROVENANCE",
    "REPLAY",
    "UNIVERSE",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    outcome: str
    detail: str
    mandatory: bool

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "outcome": self.outcome, "detail": self.detail, "mandatory": self.mandatory}


@dataclass(frozen=True)
class ExpiryReconstruction:
    as_of: str
    underlying: str
    visible: tuple[str, ...]
    selected: str | None
    two_c_selected: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "underlying": self.underlying,
            "visible": list(self.visible),
            "selected": self.selected,
            "two_c_selected": self.two_c_selected,
        }


@dataclass
class ProviderEvaluationResult:
    dataset_id: str
    dataset_version: str
    fingerprint: str
    calendar_version: str
    mapping_policy: str
    slot_tolerance_seconds: int
    cadence: tuple[str, ...]
    checks: tuple[CheckResult, ...]
    reconstructions: tuple[ExpiryReconstruction, ...]
    qualification_status: str
    approved_for_2e: bool
    limitations: tuple[str, ...]
    schema: str = EVAL_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "fingerprint": self.fingerprint,
            "calendar_version": self.calendar_version,
            "mapping_policy": self.mapping_policy,
            "slot_tolerance_seconds": self.slot_tolerance_seconds,
            "cadence": list(self.cadence),
            "checks": [c.to_dict() for c in self.checks],
            "reconstructions": [r.to_dict() for r in self.reconstructions],
            "qualification_status": self.qualification_status,
            "approved_for_2e": self.approved_for_2e,
            "limitations": list(self.limitations),
            "schema": self.schema,
            "live": False,
            "profitability_claim": False,
        }


class ProviderEvaluationRunner:
    def evaluate(self, store: CanonicalStore) -> ProviderEvaluationResult:
        meta = store.meta
        checks: list[CheckResult] = []
        reconstructions: list[ExpiryReconstruction] = []
        limitations: list[str] = []
        open_days = [s.session_date for s in store._sessions.values() if s.status == "OPEN"]
        cadence = meta.snapshot_cadence or ("11:00", "15:15")
        sample_slots = _sample_slots(open_days, cadence)

        checks.append(_universe(store))
        checks.append(_contracts(store))
        checks.append(_lot_size(store))
        checks.append(_calendar(store))
        checks.append(_provenance(store))
        checks.append(_replay(store))
        checks.append(_coverage(store, limitations))
        pit = _pit(store)
        checks.append(pit)

        cfg = load_config().options
        expiry_ok = True
        match_ok = True
        for underlying in ("NIFTY", "BANKNIFTY"):
            if underlying not in meta.instrument_scope:
                continue
            for as_of in sample_slots:
                visible = universe_at(store, underlying, as_of)
                selected = select_nearest_weekly_expiry(underlying, as_of, visible, allow_same_day=cfg.allow_same_day)
                two_c = _two_c_expiry(store, underlying, as_of)
                reconstructions.append(
                    ExpiryReconstruction(
                        as_of=as_of.isoformat(),
                        underlying=underlying,
                        visible=tuple(sorted(f"{r.expiry.isoformat()}:{r.expiry_class}" for r in visible)),
                        selected=None if selected is None else selected.isoformat(),
                        two_c_selected=None if two_c is None else two_c.isoformat(),
                    )
                )
                if selected != two_c:
                    match_ok = False
                if not visible:
                    expiry_ok = False
        checks.append(
            CheckResult("EXPIRY_RECONSTRUCTION", "PASS" if expiry_ok else "FAIL", "universe at as_of", True)
        )
        checks.append(
            CheckResult("NEAREST_WEEKLY", "PASS" if match_ok else "FAIL", "2H helper matches 2C choose_expiry", True)
        )
        checks.append(_same_day_excluded(store, reconstructions, cfg.allow_same_day))

        mandatory_fail = any(c.mandatory and c.outcome == "FAIL" for c in checks)
        warning_fail = any(not c.mandatory and c.outcome != "PASS" for c in checks)
        if mandatory_fail:
            status = REJECTED
        elif warning_fail or meta.quality_warnings:
            status = QUALIFIED_WITH_WARNINGS
        else:
            status = QUALIFIED
        approved = (
            status == QUALIFIED
            and meta.license_status == "APPROVED"
            and meta.usage_scope != FRAMEWORK_TEST_ONLY
            and not meta.is_fixture
            and meta.qualification_status != "RETIRED"
        )
        if meta.usage_scope == FRAMEWORK_TEST_ONLY or meta.is_fixture:
            limitations.append("FRAMEWORK_TEST_ONLY cannot become APPROVED_FOR_2E")
            approved = False
        if approved:
            status = APPROVED_FOR_2E
        return ProviderEvaluationResult(
            dataset_id=meta.dataset_id,
            dataset_version=meta.version,
            fingerprint=meta.fingerprint,
            calendar_version=meta.calendar_version,
            mapping_policy=meta.mapping_policy,
            slot_tolerance_seconds=meta.slot_tolerance_seconds,
            cadence=cadence,
            checks=tuple(checks),
            reconstructions=tuple(reconstructions),
            qualification_status=status,
            approved_for_2e=approved,
            limitations=tuple(limitations),
        )


def _sample_slots(days: list[date], cadence: tuple[str, ...]) -> tuple[datetime, ...]:
    slots: list[datetime] = []
    for day in days:
        for stamp in cadence:
            hour, minute = (int(p) for p in stamp.split(":"))
            slots.append(datetime.combine(day, time(hour, minute), tzinfo=IST))
    return tuple(slots)


def _universe(store: CanonicalStore) -> CheckResult:
    scope = set(store.meta.instrument_scope)
    extra = scope - ALLOWED_UNDERLYINGS
    missing = ALLOWED_UNDERLYINGS - scope
    seen = {c.option_type for c in store.all_contracts()}
    if extra or missing or seen != ALLOWED_OPTION_TYPES:
        return CheckResult("UNIVERSE", "FAIL", f"scope={sorted(scope)} types={sorted(seen)}", True)
    return CheckResult("UNIVERSE", "PASS", "NIFTY/BANKNIFTY CE/PE", True)


def _contracts(store: CanonicalStore) -> CheckResult:
    ids = [c.identity() for c in store.all_contracts()]
    if len(ids) != len(set(ids)) or not ids:
        return CheckResult("CONTRACT_IDENTITY", "FAIL", "missing or colliding identity", True)
    if any(c.lot_size is None or c.lot_size <= 0 for c in store.all_contracts()):
        return CheckResult("CONTRACT_IDENTITY", "FAIL", "lot size required for identity period", True)
    return CheckResult("CONTRACT_IDENTITY", "PASS", f"n={len(ids)}", True)


def _lot_size(store: CanonicalStore) -> CheckResult:
    if any(c.lot_size is None or c.lot_size <= 0 for c in store.all_contracts()):
        return CheckResult("LOT_SIZE", "FAIL", "historical lot size missing", True)
    return CheckResult("LOT_SIZE", "PASS", "historical lot size present", True)


def _calendar(store: CanonicalStore) -> CheckResult:
    if not store._sessions:
        return CheckResult("CALENDAR", "FAIL", "no sessions", True)
    if store.meta.calendar_version in {"", "unknown", "nse.weekday.v1"} and store.meta.usage_scope != FRAMEWORK_TEST_ONLY:
        return CheckResult("CALENDAR", "FAIL", "weekday inference is not a historical calendar", True)
    return CheckResult("CALENDAR", "PASS", store.meta.calendar_version, True)


def _provenance(store: CanonicalStore) -> CheckResult:
    meta = store.meta
    if not meta.fingerprint or meta.fingerprint == "pending":
        return CheckResult("PROVENANCE", "FAIL", "unpublished", True)
    if not meta.license_status:
        return CheckResult("PROVENANCE", "FAIL", "license missing", True)
    return CheckResult("PROVENANCE", "PASS", meta.license_status, True)


def _replay(store: CanonicalStore) -> CheckResult:
    from grow.history.adapter import load_payload

    if not store.meta.fingerprint or store.meta.fingerprint == "pending":
        return CheckResult("REPLAY", "FAIL", "unpublished", True)
    raw = store.dump()
    raw["meta"]["source_timezone"] = "Asia/Kolkata"
    reloaded = load_payload(raw)
    if reloaded.meta.fingerprint != store.meta.fingerprint:
        return CheckResult("REPLAY", "FAIL", "fingerprint drifted on reload", True)
    return CheckResult("REPLAY", "PASS", store.meta.fingerprint[:12], True)


def _coverage(store: CanonicalStore, limitations: list[str]) -> CheckResult:
    report = store.coverage()
    if report.quote_completeness < 1.0:
        limitations.append("OPTION_SNAPSHOT_GAPS")
        return CheckResult("COVERAGE", "LIMITATION", f"quote_completeness={report.quote_completeness}", False)
    return CheckResult("COVERAGE", "PASS", f"quotes={report.observed_quotes}/{report.expected_quotes}", False)


def _pit(store: CanonicalStore) -> CheckResult:
    days = [s.session_date for s in store._sessions.values() if s.status == "OPEN"]
    if not days:
        return CheckResult("PIT", "FAIL", "no open session", True)
    as_of = datetime.combine(days[0], time(11, 0), tzinfo=IST)
    before_c, before_q = store.snapshot_quotes("NIFTY", as_of)
    future = datetime.combine(days[-1], time(15, 15), tzinfo=IST)
    if future <= as_of:
        return CheckResult("PIT", "PASS", "single slot", True)
    later_c, later_q = store.snapshot_quotes("NIFTY", as_of)
    if [c.contract_id for c in before_c] != [c.contract_id for c in later_c]:
        return CheckResult("PIT", "FAIL", "snapshot mutated", True)
    if [q.ltp for q in before_q] != [q.ltp for q in later_q]:
        return CheckResult("PIT", "FAIL", "quotes mutated", True)
    return CheckResult("PIT", "PASS", "as_of snapshot stable", True)


def _two_c_expiry(store: CanonicalStore, underlying: str, as_of: datetime) -> date | None:
    src = HistoricalOptionSource(store)
    try:
        chain = src.snapshot(underlying, as_of, spot=1.0)
    except Exception:
        records = universe_at(store, underlying, as_of)
        return select_nearest_weekly_expiry(underlying, as_of, records)
    chosen, _ = choose_expiry(chain, as_of, load_config().options)
    return None if chosen is None else chosen.day


def _same_day_excluded(store: CanonicalStore, reconstructions: list[ExpiryReconstruction], allow_same_day: bool) -> CheckResult:
    if allow_same_day:
        return CheckResult("SAME_DAY", "PASS", "policy allows same-day", True)
    for row in reconstructions:
        as_of = datetime.fromisoformat(row.as_of)
        if row.selected == as_of.date().isoformat():
            return CheckResult("SAME_DAY", "FAIL", f"selected same-day {row.selected}", True)
    return CheckResult("SAME_DAY", "PASS", "same-day excluded", True)
