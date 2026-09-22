"""Immutable canonical historical store for one dataset version."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

from grow.errors import GrowConfigError
from grow.history.fingerprint import fingerprint
from grow.history.models import (
    ALLOWED_UNDERLYINGS,
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
        payload = {
            "meta": {k: v for k, v in self.meta.to_dict().items() if k != "fingerprint"},
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
        self.meta = replace(self.meta, fingerprint=fp)
        self._published = True
        return self.meta

    def coverage(self) -> CoverageReport:
        open_days = [s for s in self._sessions.values() if s.status == "OPEN"]
        quotes = self._quotes
        n = max(len(quotes), 1)
        bid_ask = sum(1 for q in quotes if q.bid is not None and q.ask is not None)
        oi = sum(1 for q in quotes if q.open_interest >= 0)
        vol = sum(1 for q in quotes if q.volume >= 0)
        iv = sum(1 for q in quotes if q.implied_volatility is not None)
        greeks = sum(1 for q in quotes if q.delta is not None)
        return CoverageReport(
            dataset_version=self.meta.version,
            expected_sessions=len(self._sessions),
            actual_sessions=len(open_days),
            missing_sessions=(),
            missing_bar_intervals=(),
            missing_option_snapshots=(),
            quote_completeness=round(len(quotes) / n, 4),
            bid_ask_completeness=round(bid_ask / n, 4),
            oi_completeness=round(oi / n, 4),
            volume_completeness=round(vol / n, 4),
            iv_completeness=round(iv / n, 4),
            greek_completeness=round(greeks / n, 4),
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
            ]
            if eligible:
                quotes.append(max(eligible, key=lambda item: item.timestamp))
        return tuple(live), tuple(quotes)

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
