"""Phase 12 — connect walk-forward validation to real historical options PIT data.

Builds point-in-time agent snapshots from a CanonicalStore contract master and
timestamped quotes, then runs chronological TRAIN → VALIDATION → TEST through
WalkForwardValidationRunner. Never uses today's chain to reconstruct a
historical decision. Never claims profitability from FIXTURE / SYNTHETIC data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Mapping, Sequence

from grow.campaign.config import campaign_paper_config
from grow.clock import IST
from grow.config import GrowConfig, load_config
from grow.errors import GrowConfigError
from grow.history.bridge import HistoricalMarketSource, HistoricalOptionSource
from grow.history.models import HISTORICAL_RESEARCH
from grow.history.store import CanonicalStore
from grow.market_data.normalized.models import (
    SCHEMA,
    AgentMarketSnapshot,
    DataQualityStatus,
    OptionQuoteView,
    UnderlyingQuoteView,
    snapshot_digest,
)
from grow.market_data.provenance import (
    MarketDataSource,
    classify_agent_snapshot,
    classify_fixture_flags,
)
from grow.validation.availability import assert_historical_not_today
from grow.validation.labels import (
    EvaluationLabel,
    assert_no_fixture_profitability_claim,
    assert_single_evaluation_label,
    classify_store_evaluation_label,
    label_payload,
    parse_evaluation_label,
    require_historical_evaluation_dataset,
)
from grow.validation.pit import as_ist, filter_quotes_pit
from grow.validation.replay import HistoricalCycle
from grow.validation.runner import WalkForwardValidationResult, WalkForwardValidationRunner


HISTORICAL_VALIDATION_VERSION = "grow.validation.historical.v1"
HISTORICAL_FIXTURE_PROVENANCE_FORBIDDEN = "HISTORICAL_FIXTURE_PROVENANCE_FORBIDDEN"
HISTORICAL_MIXED_PROVENANCE_FORBIDDEN = "HISTORICAL_MIXED_PROVENANCE_FORBIDDEN"
HISTORICAL_FIXTURE_QUOTE_FORBIDDEN = "HISTORICAL_FIXTURE_QUOTE_FORBIDDEN"
HISTORICAL_PROVENANCE_MIXED_ACROSS_CYCLES = "HISTORICAL_PROVENANCE_MIXED_ACROSS_CYCLES"


def snapshot_option_provenance(snapshot: AgentMarketSnapshot) -> MarketDataSource:
    """Classify option provenance from the actual snapshot quotes (not store meta)."""

    contracts = tuple(snapshot.option_contracts or ())
    if contracts:
        return classify_fixture_flags(bool(row.is_fixture) for row in contracts)
    source = getattr(snapshot, "market_data_source", None)
    if isinstance(source, MarketDataSource):
        return source
    return classify_agent_snapshot(snapshot)


def assert_historical_evaluation_provenance(
    evaluation_label: str | EvaluationLabel,
    cycles: Sequence[HistoricalCycle],
) -> MarketDataSource | None:
    """Fail closed when a HISTORICAL evaluation consumes fixture or mixed PIT data.

    Inspects every cycle snapshot's option-quote provenance. Store metadata alone
    is not sufficient: FIXTURE / MIXED quotes, or provenance that differs across
    cycles, reject the entire evaluation.
    """

    label = parse_evaluation_label(evaluation_label)
    if label is not EvaluationLabel.HISTORICAL:
        return None
    if not cycles:
        raise GrowConfigError("HISTORICAL_NO_CYCLES")

    sources: list[MarketDataSource] = []
    for cycle in cycles:
        snap = cycle.snapshot
        source = snapshot_option_provenance(snap)
        sources.append(source)
        if any(bool(row.is_fixture) for row in snap.option_contracts) and source is MarketDataSource.LIVE:
            # Inconsistent stamp: fixture flags present but classification says LIVE.
            raise GrowConfigError(HISTORICAL_FIXTURE_QUOTE_FORBIDDEN)

    unique = {item.value for item in sources}
    if len(unique) > 1:
        raise GrowConfigError(HISTORICAL_PROVENANCE_MIXED_ACROSS_CYCLES)

    only = sources[0]
    if only is MarketDataSource.FIXTURE:
        raise GrowConfigError(HISTORICAL_FIXTURE_PROVENANCE_FORBIDDEN)
    if only is MarketDataSource.MIXED:
        raise GrowConfigError(HISTORICAL_MIXED_PROVENANCE_FORBIDDEN)
    if any(bool(cycle.snapshot.is_fixture) for cycle in cycles):
        raise GrowConfigError(HISTORICAL_FIXTURE_PROVENANCE_FORBIDDEN)
    if any(bool(row.is_fixture) for cycle in cycles for row in cycle.snapshot.option_contracts):
        raise GrowConfigError(HISTORICAL_FIXTURE_QUOTE_FORBIDDEN)
    if only is not MarketDataSource.LIVE:
        raise GrowConfigError(f"HISTORICAL_NON_LIVE_PROVENANCE:{only.value}")
    return only



@dataclass(frozen=True)
class ListedHistoricalContract:
    """Contract master row visible to availability checks at decision time."""

    underlying: str
    expiry: date
    strike: float
    option_type: str
    first_seen_at: datetime
    last_seen_at: datetime
    lot_size: int | None
    contract_id: str
    provider_contract_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type,
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "lot_size": self.lot_size,
            "contract_id": self.contract_id,
            "provider_contract_id": self.provider_contract_id,
        }


@dataclass
class HistoricalValidationResult:
    evaluation_label: EvaluationLabel
    walk_forward: WalkForwardValidationResult
    dataset_id: str
    dataset_version: str
    dataset_fingerprint: str
    provider_name: str
    sessions: tuple[date, ...]
    cycle_count: int
    contract_master_size: int
    today_chain_guard: str
    profitability_claim: bool = False

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema": HISTORICAL_VALIDATION_VERSION,
            "evaluation_label": self.evaluation_label.value,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_fingerprint": self.dataset_fingerprint,
            "provider_name": self.provider_name,
            "sessions": [day.isoformat() for day in self.sessions],
            "cycle_count": self.cycle_count,
            "contract_master_size": self.contract_master_size,
            "today_chain_guard": self.today_chain_guard,
            "walk_forward": self.walk_forward.to_dict(),
            **label_payload(self.evaluation_label),
        }
        body["profitability_claim"] = False
        return body


def listed_contracts_from_store(store: CanonicalStore) -> dict[str, ListedHistoricalContract]:
    """Map provider + canonical ids to PIT-listed contracts (no today chain)."""

    listed: dict[str, ListedHistoricalContract] = {}
    for raw in store.all_contracts():
        row = ListedHistoricalContract(
            underlying=raw.underlying,
            expiry=raw.expiry,
            strike=float(raw.strike),
            option_type=raw.option_type,
            first_seen_at=raw.first_seen_at.astimezone(IST),
            last_seen_at=raw.last_seen_at.astimezone(IST),
            lot_size=raw.lot_size,
            contract_id=raw.contract_id,
            provider_contract_id=raw.provider_contract_id,
        )
        listed[raw.provider_contract_id] = row
        listed[raw.contract_id] = row
        alt = f"{raw.underlying}-{raw.expiry.isoformat()}-{int(raw.strike)}-{raw.option_type}"
        listed[alt] = row
    return listed


def decision_slots(store: CanonicalStore) -> tuple[datetime, ...]:
    """Chronological decision timestamps from open sessions × snapshot cadence."""

    cadence = store.meta.snapshot_cadence or ("11:00",)
    slots: list[datetime] = []
    for session in sorted(store._sessions.values(), key=lambda s: s.session_date):
        if session.status != "OPEN":
            continue
        for token in cadence:
            hour, minute = (int(part) for part in str(token).split(":", 1))
            moment = datetime.combine(session.session_date, time(hour, minute), tzinfo=IST)
            if session.open_at <= moment <= session.close_at:
                slots.append(moment)
    return tuple(slots)


def build_historical_agent_snapshot(
    store: CanonicalStore,
    *,
    underlying: str,
    as_of: datetime,
    exchange: str = "NSE",
) -> AgentMarketSnapshot:
    """Build an agent snapshot from PIT historical bars + option quotes only."""

    moment = as_ist(as_of)
    market_src = HistoricalMarketSource(store)
    option_src = HistoricalOptionSource(store)
    market = market_src.snapshot(underlying, as_of=moment)
    spot = float(market.last_price)
    chain = option_src.snapshot(underlying, moment, spot=spot)

    # Fail closed if a caller tried to inject a live/today universe.
    historical_ids = {
        c.provider_contract_id for c in store.contracts_at(underlying, moment)
    } | {c.contract_id for c in store.contracts_at(underlying, moment)}
    chain_ids = {c.provider_contract_id for c in chain.contracts}
    assert_historical_not_today(historical_ids, chain_ids)

    store_fixture = bool(store.meta.is_fixture) or bool(chain.is_fixture)
    hist_contracts, hist_quotes = store.snapshot_quotes(underlying, moment)
    hist_by_provider = {c.provider_contract_id: c for c in hist_contracts}
    hist_by_id = {c.contract_id: c for c in hist_contracts}
    quote_by_cid = {q.contract_id: q for q in hist_quotes}
    options: list[OptionQuoteView] = []
    for contract in chain.contracts:
        age = (moment - contract.timestamp.astimezone(IST)).total_seconds()
        if age < 0:
            raise GrowConfigError("LOOKAHEAD_OPTION_QUOTE")
        lot = None
        resolved = hist_by_provider.get(contract.provider_contract_id)
        if resolved is None:
            try:
                resolved = store.contract_for_candidate(
                    underlying=contract.underlying,
                    expiry=contract.expiry,
                    strike=float(contract.strike),
                    option_type=contract.option_type.value,
                    as_of=moment,
                    provider_contract_id=contract.provider_contract_id,
                )
            except GrowConfigError:
                resolved = None
        if resolved is not None:
            lot = resolved.lot_size
            hist_by_id.setdefault(resolved.contract_id, resolved)
        raw_quote = None
        if resolved is not None:
            raw_quote = quote_by_cid.get(resolved.contract_id)
        # Per-quote provenance: store/chain fixture OR explicit FIXTURE quality flag.
        quote_fixture = store_fixture
        if raw_quote is not None and any(str(flag).upper() == "FIXTURE" for flag in raw_quote.quality_flags):
            quote_fixture = True
        options.append(
            OptionQuoteView(
                underlying=contract.underlying,
                expiry=contract.expiry,
                strike=float(contract.strike),
                option_type=contract.option_type.value,
                ltp=contract.last_price,
                bid=contract.bid,
                ask=contract.ask,
                open_interest=contract.open_interest,
                volume=contract.volume,
                quote_timestamp=contract.timestamp.astimezone(IST),
                quote_age_seconds=age,
                provider_contract_id=contract.provider_contract_id,
                quality=DataQualityStatus.OK,
                lot_size=lot,
                previous_open_interest=contract.previous_open_interest,
                implied_volatility=contract.implied_volatility,
                delta=contract.delta,
                gamma=contract.gamma,
                theta=contract.theta,
                vega=contract.vega,
                expiry_class=contract.expiry_class.value if contract.expiry_class is not None else None,
                is_fixture=quote_fixture,
            )
        )
    pit_options = filter_quotes_pit(options, moment)
    if not pit_options:
        raise GrowConfigError("DATA_UNAVAILABLE:NO_PIT_OPTION_QUOTES")

    underlyings = {
        underlying: UnderlyingQuoteView(
            underlying=underlying,
            exchange=exchange,
            spot=spot,
            ltp=spot,
            open=None,
            high=None,
            low=None,
            close=spot,
            volume=None,
            quote_timestamp=moment,
            quote_age_seconds=0.0,
        )
    }
    source = (
        classify_fixture_flags(row.is_fixture for row in pit_options)
        if pit_options
        else (MarketDataSource.FIXTURE if store_fixture else MarketDataSource.LIVE)
    )
    version = snapshot_digest(
        {
            "as_of": moment.isoformat(),
            "underlying": underlying,
            "spot": spot,
            "options": [c.to_dict() for c in pit_options],
            "dataset": store.meta.fingerprint,
        }
    )
    return AgentMarketSnapshot(
        snapshot_id=f"hist-{version}",
        version=version,
        schema=SCHEMA,
        provider=store.meta.provider_name,
        exchange=exchange,
        session_timestamp=moment,
        decision_timestamp=moment,
        session_date=moment.date(),
        underlyings=underlyings,
        option_contracts=pit_options,
        data_quality=DataQualityStatus.OK,
        quality_notes=("HISTORICAL_PIT", store.meta.fingerprint[:12]),
        source_snapshot_ids={
            "market": market.snapshot_id,
            "chain": chain.snapshot_id,
            "dataset": store.meta.fingerprint,
        },
        diagnostics={
            "fixture": source is MarketDataSource.FIXTURE,
            "market_data_source": source.value,
            "evaluation_dataset_id": store.meta.dataset_id,
            "evaluation_dataset_version": store.meta.version,
            "historical_validation_version": HISTORICAL_VALIDATION_VERSION,
            "contract_master_size": len(historical_ids),
        },
        is_fixture=source is MarketDataSource.FIXTURE,
        market_data_source=source,
    )


def build_historical_cycles(
    store: CanonicalStore,
    *,
    underlying: str,
    packages: Mapping[str, Any] | None = None,
    regime: str = "UNKNOWN",
) -> tuple[HistoricalCycle, ...]:
    """One HistoricalCycle per PIT decision slot (chronological, no lookahead)."""

    out: list[HistoricalCycle] = []
    package_map = dict(packages or {})
    for slot in decision_slots(store):
        snap = build_historical_agent_snapshot(store, underlying=underlying, as_of=slot)
        key = slot.isoformat()
        package = package_map.get(key) or package_map.get(slot.date().isoformat())
        out.append(
            HistoricalCycle(
                snapshot=snap,
                package=package,
                session_date=slot.date(),
                regime=regime,
            )
        )
    out.sort(key=lambda c: c.snapshot.decision_timestamp.astimezone(IST))
    return tuple(out)


def open_session_dates(store: CanonicalStore) -> tuple[date, ...]:
    return tuple(
        sorted(s.session_date for s in store._sessions.values() if s.status == "OPEN")
    )


class HistoricalValidationCampaign:
    """Walk-forward validation driven by a real historical options PIT store."""

    def __init__(
        self,
        store: CanonicalStore,
        *,
        risk_secret: str,
        config: GrowConfig | None = None,
        qualification: Mapping[str, Any] | None = None,
        evaluation_label: str | EvaluationLabel | None = None,
        underlying: str = "NIFTY",
        apply_campaign_profile: bool = True,
        today_universe: set[str] | None = None,
    ) -> None:
        base = config or load_config()
        if apply_campaign_profile:
            base = campaign_paper_config(base)
        base.assert_safe()
        if base.execution.live_trading_enabled or base.live_data.live_trading:
            raise GrowConfigError("LIVE_TRADING_FORBIDDEN")
        if not base.live_data.paper_mode:
            raise GrowConfigError("PAPER_MODE_REQUIRED")

        derived = classify_store_evaluation_label(store)
        if evaluation_label is None:
            label = derived
        else:
            label = parse_evaluation_label(evaluation_label)

        if label is EvaluationLabel.LIVE_PAPER:
            raise GrowConfigError("LIVE_PAPER_NOT_HISTORICAL_VALIDATION")

        if evaluation_label is not None:
            assert_single_evaluation_label((label, derived))

        if label is EvaluationLabel.HISTORICAL:
            require_historical_evaluation_dataset(store, qualification)
        else:
            # FIXTURE / SYNTHETIC allowed for framework tests only — never as HISTORICAL.
            if store.meta.usage_scope == HISTORICAL_RESEARCH and not store.meta.is_fixture:
                raise GrowConfigError("HISTORICAL_LABEL_REQUIRED")

        self.store = store
        self.config = base
        self.risk_secret = risk_secret
        self.qualification = dict(qualification or {})
        self.evaluation_label = label
        self.underlying = underlying
        self.today_universe = set(today_universe or ())
        self.listed = listed_contracts_from_store(store)

        # Guard: never silently substitute a live/today contract master.
        historical_universe = set(self.listed)
        if self.today_universe:
            assert_historical_not_today(historical_universe, self.today_universe)

    def run(
        self,
        *,
        packages: Mapping[str, Any] | None = None,
        include_fee_stress: bool = True,
        sessions: Sequence[date] | None = None,
    ) -> HistoricalValidationResult:
        assert_no_fixture_profitability_claim(self.evaluation_label, profitability_claim=False)

        session_days = tuple(sessions) if sessions is not None else open_session_dates(self.store)
        if not session_days:
            raise GrowConfigError("HISTORICAL_NO_SESSIONS")

        cycles = build_historical_cycles(
            self.store,
            underlying=self.underlying,
            packages=packages,
        )
        if not cycles:
            raise GrowConfigError("HISTORICAL_NO_CYCLES")

        # Fail closed on actual PIT option provenance — not store.meta alone.
        assert_historical_evaluation_provenance(self.evaluation_label, cycles)

        # Re-check today-chain substitution against the actual cycle universe.
        cycle_universe = {
            q.provider_contract_id
            for cycle in cycles
            for q in cycle.snapshot.option_contracts
        }
        if self.today_universe:
            assert_historical_not_today(cycle_universe, self.today_universe)

        meta = self.store.meta
        runner = WalkForwardValidationRunner(
            self.config,
            risk_secret=self.risk_secret,
            dataset_id=meta.dataset_id,
            dataset_version=meta.version,
            dataset_fingerprint=meta.fingerprint,
            provider_name=meta.provider_name,
            listed_contracts=self.listed,
            evaluation_label=self.evaluation_label.value,
        )
        result = runner.run(
            sessions=session_days,
            cycles=cycles,
            include_fee_stress=include_fee_stress,
        )
        claim = bool(result.to_dict().get("profitability_claim"))
        assert_no_fixture_profitability_claim(self.evaluation_label, profitability_claim=claim)

        return HistoricalValidationResult(
            evaluation_label=self.evaluation_label,
            walk_forward=result,
            dataset_id=meta.dataset_id,
            dataset_version=meta.version,
            dataset_fingerprint=meta.fingerprint,
            provider_name=meta.provider_name,
            sessions=session_days,
            cycle_count=len(cycles),
            contract_master_size=len(self.store.all_contracts()),
            today_chain_guard="REJECTED" if self.today_universe else "NOT_APPLICABLE",
            profitability_claim=False,
        )
