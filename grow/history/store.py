"""Immutable canonical historical store for one dataset version."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time

from grow.clock import IST

from grow.errors import GrowConfigError
from grow.history.fingerprint import fingerprint
from grow.history.models import (
    ALLOWED_UNDERLYINGS,
    APPROVED,
    APPROVED_WITH_WARNINGS,
    BID_ASK_GAPS,
    FRAMEWORK_TEST_ONLY,
    KNOWN_MAPPING_POLICIES,
    MAPPING_EXACT,
    MISSING_IV,
    MISSING_OI,
    MISSING_SESSIONS,
    MISSING_VOLUME,
    OPTION_SNAPSHOT_GAPS,
    REJECTED,
    SYNTHETIC,
    CoverageReport,
    DatasetVersion,
    HistoricalBar,
    HistoricalOptionContract,
    HistoricalOptionQuote,
    HistoricalSession,
)
from grow.history.quality import validate_bar, validate_contract, validate_quote, validate_session


class CanonicalStore:
    def __init__(self, meta: DatasetVersion) -> None:
        self.meta = meta
        if meta.mapping_policy not in KNOWN_MAPPING_POLICIES:
            raise GrowConfigError(f"UNKNOWN_MAPPING_POLICY:{meta.mapping_policy}")
        if meta.slot_tolerance_seconds < 0:
            raise GrowConfigError("SLOT_TOLERANCE")
        self._sessions: dict[date, HistoricalSession] = {}
        self._bars: dict[tuple[str, str, datetime], HistoricalBar] = {}
        self._contracts: dict[str, HistoricalOptionContract] = {}
        self._quotes: list[HistoricalOptionQuote] = []
        self._published = False

    def add_session(self, session: HistoricalSession) -> None:
        self._guard()
        validate_session(session)
        if session.session_date in self._sessions:
            raise GrowConfigError("DUPLICATE_SESSION")
        self._sessions[session.session_date] = session

    def add_bar(self, bar: HistoricalBar) -> None:
        self._guard()
        validate_bar(bar)
        key = (bar.symbol, bar.timeframe, bar.timestamp)
        if key in self._bars:
            raise GrowConfigError("DUPLICATE_BAR")
        if bar.dataset_version != self.meta.version:
            raise GrowConfigError("DATASET_VERSION")
        self._bars[key] = bar

    def add_contract(self, contract: HistoricalOptionContract) -> None:
        self._guard()
        validate_contract(contract)
        if contract.contract_id in self._contracts:
            raise GrowConfigError("DUPLICATE_CONTRACT")
        identities = {c.identity() for c in self._contracts.values()}
        if contract.identity() in identities:
            raise GrowConfigError("CONTRACT_IDENTITY_COLLISION")
        if contract.dataset_version != self.meta.version:
            raise GrowConfigError("DATASET_VERSION")
        self._contracts[contract.contract_id] = contract

    def add_quote(self, quote: HistoricalOptionQuote) -> None:
        self._guard()
        validate_quote(quote)
        if quote.contract_id not in self._contracts:
            raise GrowConfigError("UNKNOWN_CONTRACT")
        key = (quote.contract_id, quote.timestamp)
        if any((q.contract_id, q.timestamp) == key for q in self._quotes):
            raise GrowConfigError("DUPLICATE_QUOTE")
        if quote.dataset_version != self.meta.version:
            raise GrowConfigError("DATASET_VERSION")
        self._quotes.append(quote)

    def publish(self) -> DatasetVersion:
        self._guard()
        report = self.coverage()
        gated = apply_quality_gate(self.meta, report)
        payload = {
            "meta": {k: v for k, v in gated.to_dict().items() if k != "fingerprint"},
            "sessions": [self._sessions[d].to_dict() for d in sorted(self._sessions)],
            "bars": [self._bars[k].to_dict() for k in sorted(self._bars, key=lambda i: (i[0], i[1], i[2].isoformat()))],
            "contracts": [self._contracts[k].to_dict() for k in sorted(self._contracts)],
            "quotes": [
                q.to_dict()
                for q in sorted(self._quotes, key=lambda item: (item.contract_id, item.timestamp.isoformat()))
            ],
            "coverage": report.to_dict(),
        }
        fp = fingerprint(payload)
        self.meta = replace(gated, fingerprint=fp)
        self._published = True
        return self.meta

    def coverage(self) -> CoverageReport:
        open_days = [s.session_date for s in self._sessions.values() if s.status == "OPEN"]
        days_with_bars = {bar.timestamp.date() for bar in self._bars.values()}
        missing_sessions = tuple(day.isoformat() for day in sorted(open_days) if day not in days_with_bars)
        expected_quotes = 0
        observed_quotes = 0
        missing_snaps: list[str] = []
        cadence = self.meta.snapshot_cadence
        # v1 coverage is EXACT slot match. mapping_policy is recorded for future
        # vendor adapters; NEAREST_WITHIN_TOLERANCE is not applied here.
        quotes_by_slot: dict[tuple[str, datetime], HistoricalOptionQuote] = {}
        for quote in self._quotes:
            quotes_by_slot[(quote.contract_id, quote.timestamp)] = quote
        for day in open_days:
            session = self._sessions[day]
            for stamp in cadence:
                hour, minute = (int(part) for part in stamp.split(":"))
                slot = datetime.combine(day, time(hour, minute), tzinfo=IST)
                if slot < session.open_at or slot > session.close_at:
                    continue
                for contract in self._contracts.values():
                    if not (contract.first_seen_at <= slot <= contract.last_seen_at):
                        continue
                    expected_quotes += 1
                    if (contract.contract_id, slot) in quotes_by_slot:
                        observed_quotes += 1
                    else:
                        missing_snaps.append(f"{contract.contract_id}:{slot.isoformat()}")

        observed_rows = self._quotes
        n_obs = len(observed_rows)

        def _ratio(count: int, total: int) -> float:
            if total <= 0:
                return 0.0
            return round(count / total, 4)

        bid_ask = sum(1 for q in observed_rows if q.bid is not None and q.ask is not None)
        oi = sum(1 for q in observed_rows if q.open_interest is not None)
        vol = sum(1 for q in observed_rows if q.volume is not None)
        iv = sum(1 for q in observed_rows if q.implied_volatility is not None)
        greeks = sum(1 for q in observed_rows if q.delta is not None)
        return CoverageReport(
            dataset_version=self.meta.version,
            expected_sessions=len(open_days),
            actual_sessions=len({d for d in open_days if d in days_with_bars}),
            missing_sessions=missing_sessions,
            missing_bar_intervals=(),
            missing_option_snapshots=tuple(missing_snaps[:20]),
            expected_quotes=expected_quotes,
            observed_quotes=observed_quotes,
            quote_completeness=_ratio(observed_quotes, expected_quotes),
            bid_ask_completeness=_ratio(bid_ask, n_obs),
            oi_completeness=_ratio(oi, n_obs),
            volume_completeness=_ratio(vol, n_obs),
            iv_completeness=_ratio(iv, n_obs),
            greek_completeness=_ratio(greeks, n_obs),
        )

    def sessions(self, start: date, end: date) -> tuple[HistoricalSession, ...]:
        return tuple(
            self._sessions[day]
            for day in sorted(self._sessions)
            if start <= day <= end and self._sessions[day].status == "OPEN"
        )

    def bars_at(self, symbol: str, as_of: datetime, timeframe: str | None = None) -> tuple[HistoricalBar, ...]:
        if symbol not in ALLOWED_UNDERLYINGS:
            raise GrowConfigError(f"UNSUPPORTED_UNDERLYING:{symbol}")
        out = []
        for bar in self._bars.values():
            if bar.symbol != symbol:
                continue
            if timeframe and bar.timeframe != timeframe:
                continue
            if bar.as_of_available_at <= as_of and bar.end <= as_of:
                out.append(bar)
        return tuple(sorted(out, key=lambda b: (b.timeframe, b.timestamp)))

    def snapshot_quotes(self, underlying: str, as_of: datetime) -> tuple[HistoricalOptionContract, tuple[HistoricalOptionQuote, ...]]:
        if underlying not in ALLOWED_UNDERLYINGS:
            raise GrowConfigError(f"UNSUPPORTED_UNDERLYING:{underlying}")
        live = [
            c
            for c in self._contracts.values()
            if c.underlying == underlying and c.first_seen_at <= as_of <= c.last_seen_at
        ]
        quotes: list[HistoricalOptionQuote] = []
        for contract in live:
            eligible = [
                q
                for q in self._quotes
                if q.contract_id == contract.contract_id
                and q.as_of_available_at <= as_of
                and q.timestamp <= as_of
                and contract.first_seen_at <= q.timestamp <= contract.last_seen_at
            ]
            if eligible:
                quotes.append(max(eligible, key=lambda item: item.timestamp))
        return tuple(live), tuple(quotes)

    def session_on(self, day: date) -> HistoricalSession | None:
        return self._sessions.get(day)

    def has_contract(self, contract_id: str) -> bool:
        return contract_id in self._contracts

    def contract(self, contract_id: str) -> HistoricalOptionContract:
        if contract_id not in self._contracts:
            raise GrowConfigError("UNKNOWN_CONTRACT")
        return self._contracts[contract_id]

    def lot_size(self, contract_id: str) -> int:
        contract = self.contract(contract_id)
        if contract.lot_size is None:
            raise GrowConfigError("MISSING_LOT_SIZE")
        return contract.lot_size

    def _guard(self) -> None:
        if self._published:
            raise GrowConfigError("DATASET_IMMUTABLE")


def apply_quality_gate(meta: DatasetVersion, report: CoverageReport) -> DatasetVersion:
    warnings: list[str] = []
    if report.missing_sessions:
        warnings.append(MISSING_SESSIONS)
    if report.expected_quotes == 0 or report.quote_completeness < 1.0:
        warnings.append(OPTION_SNAPSHOT_GAPS)
    if meta.bid_ask_available and report.bid_ask_completeness < 1.0:
        warnings.append(BID_ASK_GAPS)
    if meta.oi_available and report.oi_completeness < 1.0:
        warnings.append(MISSING_OI)
    if meta.volume_available and report.volume_completeness < 1.0:
        warnings.append(MISSING_VOLUME)
    if meta.iv_available and report.iv_completeness < 1.0:
        warnings.append(MISSING_IV)
    coded = tuple(sorted(dict.fromkeys(warnings)))
    if meta.usage_scope == FRAMEWORK_TEST_ONLY or meta.is_fixture:
        return replace(meta, quality_status=SYNTHETIC, license_status="NOT_APPROVED", quality_warnings=coded)
    if report.observed_quotes == 0 or report.actual_sessions == 0:
        return replace(meta, quality_status=REJECTED, quality_warnings=coded)
    if coded:
        return replace(meta, quality_status=APPROVED_WITH_WARNINGS, quality_warnings=coded)
    return replace(meta, quality_status=APPROVED, quality_warnings=())

