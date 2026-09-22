"""2I synthetic provider-shaped sample.

FRAMEWORK_TEST_ONLY. Not a licensed NSE/GDF/TrueData feed.
NIFTY + BANKNIFTY (weekly preferred) and MIDCPNIFTY (monthly only).
Ten consecutive weekdays 2026-09-07 .. 2026-09-18.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from grow.clock import IST
from grow.data.schedule import bar_duration, expected_starts
from grow.data.schema import Timeframe
from grow.history.models import (
    CANDIDATE,
    FRAMEWORK_TEST_ONLY,
    NORM,
    SCHEMA,
    SYNTHETIC,
    DatasetVersion,
    HistoricalBar,
    HistoricalOptionContract,
    HistoricalOptionQuote,
    HistoricalSession,
)
from grow.history.store import CanonicalStore

SAMPLE_2I_ID = "grow.history.provider.sample.v1"
SAMPLE_2I_VERSION = "2026.09.2i.a"
CALENDAR_VERSION = "nse.session.2i.v1"
SOURCE = "grow.history.file.2i"
START = date(2026, 9, 7)
END = date(2026, 9, 18)
OPEN_T = time(9, 15)
CLOSE_T = time(15, 30)
NIFTY_WEEKLY_NEAR = date(2026, 9, 8)
NIFTY_WEEKLY_NEXT = date(2026, 9, 15)
NIFTY_WEEKLY_LATER = date(2026, 9, 22)
NIFTY_MONTHLY = date(2026, 9, 24)
MIDCP_MONTHLY = date(2026, 9, 29)


def trading_days() -> tuple[date, ...]:
    day = START
    out: list[date] = []
    while day <= END:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return tuple(out)


def _px(symbol: str, start: datetime) -> float:
    base = {"NIFTY": 25000.0, "BANKNIFTY": 52000.0, "MIDCPNIFTY": 13000.0}[symbol]
    bump = start.hour * 0.5 + start.minute * 0.01 + start.date().day
    return round(base + bump, 2)


def build_2i_store(*, publish: bool = True) -> CanonicalStore:
    days = trading_days()
    meta = DatasetVersion(
        dataset_id=SAMPLE_2I_ID,
        version=SAMPLE_2I_VERSION,
        source_version="2i-raw-1",
        normalization_version=NORM,
        schema_version=SCHEMA,
        calendar_version=CALENDAR_VERSION,
        coverage_start=START,
        coverage_end=END,
        fingerprint="pending",
        published_at="2026-01-01T08:00:00+05:30",
        quality_status=SYNTHETIC,
        license_status="NOT_APPROVED",
        source_id=SOURCE,
        provider_name="grow-2i-sample",
        granularity=("M15", "D1"),
        instrument_scope=("NIFTY", "BANKNIFTY", "MIDCPNIFTY"),
        bid_ask_available=True,
        oi_available=True,
        volume_available=True,
        iv_available=True,
        greeks_available=False,
        contract_metadata_available=True,
        option_depth="atm_pm2",
        provenance="SYNTHETIC 2I provider-shaped sample — not licensed historical research",
        timezone="Asia/Kolkata",
        usage_scope=FRAMEWORK_TEST_ONLY,
        is_fixture=True,
        snapshot_cadence=("11:00", "15:15"),
        quality_warnings=(),
        mapping_policy="EXACT",
        slot_tolerance_seconds=0,
        qualification_status=CANDIDATE,
    )
    store = CanonicalStore(meta)
    for day in days:
        open_at = datetime.combine(day, OPEN_T, tzinfo=IST)
        close_at = datetime.combine(day, CLOSE_T, tzinfo=IST)
        store.add_session(
            HistoricalSession(
                session_date=day,
                open_at=open_at,
                close_at=close_at,
                source=SOURCE,
                calendar_version=CALENDAR_VERSION,
                status="OPEN",
                special_reason="high_volatility" if day == date(2026, 9, 16) else ("quiet" if day == date(2026, 9, 17) else None),
            )
        )
        for symbol in ("NIFTY", "BANKNIFTY", "MIDCPNIFTY"):
            dur = bar_duration(Timeframe.M15)
            for start in expected_starts(day, Timeframe.M15, session_open=OPEN_T, session_close=CLOSE_T):
                end = start + dur
                px = _px(symbol, start)
                store.add_bar(
                    HistoricalBar(
                        symbol=symbol,
                        timeframe="M15",
                        timestamp=start,
                        end=end,
                        open=px,
                        high=px + 2,
                        low=px - 2,
                        close=px + 0.5,
                        volume=1000,
                        source_id=SOURCE,
                        dataset_version=SAMPLE_2I_VERSION,
                        as_of_available_at=end,
                        corporate_action_adjustment_version="unadjusted.v1",
                        quality_flags=(),
                    )
                )
            px = _px(symbol, open_at)
            store.add_bar(
                HistoricalBar(
                    symbol=symbol,
                    timeframe="D1",
                    timestamp=open_at,
                    end=close_at,
                    open=px,
                    high=px + 20,
                    low=px - 20,
                    close=px + 5,
                    volume=100000,
                    source_id=SOURCE,
                    dataset_version=SAMPLE_2I_VERSION,
                    as_of_available_at=close_at,
                    corporate_action_adjustment_version="unadjusted.v1",
                    quality_flags=(),
                )
            )
            _contracts(store, symbol, day, open_at)
    if publish:
        store.publish()
    return store


def _contracts(store: CanonicalStore, symbol: str, day: date, open_at: datetime) -> None:
    first_week = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
    if symbol == "MIDCPNIFTY":
        specs = (
            (MIDCP_MONTHLY, "MONTHLY", first_week, datetime(2026, 9, 29, 15, 30, tzinfo=IST), 13000.0, 25.0, 75),
        )
    else:
        atm = 25000.0 if symbol == "NIFTY" else 52000.0
        step = 50.0 if symbol == "NIFTY" else 100.0
        lot = 75 if symbol == "NIFTY" else 15
        specs = (
            (NIFTY_WEEKLY_NEAR, "WEEKLY", first_week, datetime(2026, 9, 8, 15, 30, tzinfo=IST), atm, step, lot),
            (NIFTY_WEEKLY_NEXT, "WEEKLY", first_week, datetime(2026, 9, 15, 15, 30, tzinfo=IST), atm, step, lot),
            (NIFTY_WEEKLY_LATER, "WEEKLY", datetime(2026, 9, 16, 9, 15, tzinfo=IST), datetime(2026, 9, 22, 15, 30, tzinfo=IST), atm, step, lot),
            (NIFTY_MONTHLY, "MONTHLY", first_week, datetime(2026, 9, 24, 15, 30, tzinfo=IST), atm, step, lot),
        )
    for expiry, klass, first, last, atm, step, lot in specs:
        if open_at < first or open_at > last:
            continue
        for k in range(-2, 3):
            strike = atm + k * step
            for kind in ("CE", "PE"):
                cid = f"{symbol}-{expiry.isoformat()}-{int(strike)}-{kind}"
                if not store.has_contract(cid):
                    store.add_contract(
                        HistoricalOptionContract(
                            underlying=symbol,
                            expiry=expiry,
                            strike=strike,
                            option_type=kind,
                            contract_id=cid,
                            provider_contract_id=cid,
                            lot_size=lot,
                            expiry_class=klass,
                            first_seen_at=first,
                            last_seen_at=last,
                            listing_status="ACTIVE",
                            source_id=SOURCE,
                            dataset_version=SAMPLE_2I_VERSION,
                        )
                    )
                for hh, mm in ((11, 0), (15, 15)):
                    ts = datetime.combine(day, time(hh, mm), tzinfo=IST)
                    if not (first <= ts <= last):
                        continue
                    prem = 80.0 + abs(k) * 10
                    store.add_quote(
                        HistoricalOptionQuote(
                            contract_id=cid,
                            timestamp=ts,
                            bid=prem - 0.5,
                            ask=prem + 0.5,
                            ltp=prem,
                            volume=200,
                            open_interest=5000,
                            previous_open_interest=4800,
                            implied_volatility=0.12,
                            delta=None,
                            gamma=None,
                            theta=None,
                            vega=None,
                            greek_source=None,
                            iv_source="PROVIDER",
                            source_id=SOURCE,
                            dataset_version=SAMPLE_2I_VERSION,
                            as_of_available_at=ts,
                            quality_flags=(),
                        )
                    )
