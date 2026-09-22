"""Synthetic point-in-time historical sample. Not a licensed vendor feed."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from grow.clock import IST
from grow.data.schedule import bar_duration, expected_starts
from grow.data.schema import Timeframe
from grow.history.models import (
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

SAMPLE_ID = "grow.history.sample.v1"
SAMPLE_VERSION = "2026.09.sample.a"
CALENDAR_VERSION = "nse.session.sample.v1"
SOURCE = "grow.history.file.sample"
HOLIDAY = date(2026, 9, 8)
START = date(2026, 9, 1)
END = date(2026, 9, 18)
OPEN_T = time(9, 15)
CLOSE_T = time(15, 30)


def _weekdays(start: date, end: date) -> list[date]:
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _px(symbol: str, start: datetime) -> float:
    base = 25000.0 if symbol == "NIFTY" else 52000.0
    bump = start.hour * 0.5 + start.minute * 0.01 + start.date().day
    return round(base + bump, 2)


def build_sample_store() -> CanonicalStore:
    meta = DatasetVersion(
        dataset_id=SAMPLE_ID,
        version=SAMPLE_VERSION,
        source_version="sample-raw-1",
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
        provider_name="grow-sample",
        granularity=("M5", "M15", "D1"),
        instrument_scope=("NIFTY", "BANKNIFTY"),
        bid_ask_available=True,
        oi_available=True,
        volume_available=True,
        iv_available=True,
        greeks_available=False,
        contract_metadata_available=True,
        option_depth="atm_pm2",
        provenance="SYNTHETIC FRAMEWORK_TEST_ONLY — not historical research",
        timezone="Asia/Kolkata",
        usage_scope=FRAMEWORK_TEST_ONLY,
        is_fixture=True,
        snapshot_cadence=("11:00", "15:15"),
    )
    store = CanonicalStore(meta)
    for day in _weekdays(START, END):
        open_at = datetime.combine(day, OPEN_T, tzinfo=IST)
        close_at = datetime.combine(day, CLOSE_T, tzinfo=IST)
        closed = day == HOLIDAY
        store.add_session(
            HistoricalSession(
                session_date=day,
                open_at=open_at,
                close_at=close_at,
                source=SOURCE,
                calendar_version=CALENDAR_VERSION,
                status="CLOSED" if closed else "OPEN",
                special_reason="holiday" if closed else None,
            )
        )
        if closed:
            continue
        for symbol in ("NIFTY", "BANKNIFTY"):
            for tf in (Timeframe.M5, Timeframe.M15):
                dur = bar_duration(tf)
                for start in expected_starts(day, tf, session_open=OPEN_T, session_close=CLOSE_T):
                    end = start + dur
                    px = _px(symbol, start)
                    store.add_bar(
                        HistoricalBar(
                            symbol=symbol,
                            timeframe=tf.value,
                            timestamp=start,
                            end=end,
                            open=px,
                            high=px + 2,
                            low=px - 2,
                            close=px + 0.5,
                            volume=1000,
                            source_id=SOURCE,
                            dataset_version=SAMPLE_VERSION,
                            as_of_available_at=end,
                            corporate_action_adjustment_version="unadjusted.v1",
                            quality_flags=(),
                        )
                    )
            d1_end = close_at
            px = _px(symbol, open_at)
            store.add_bar(
                HistoricalBar(
                    symbol=symbol,
                    timeframe="D1",
                    timestamp=open_at,
                    end=d1_end,
                    open=px,
                    high=px + 20,
                    low=px - 20,
                    close=px + 5,
                    volume=100000,
                    source_id=SOURCE,
                    dataset_version=SAMPLE_VERSION,
                    as_of_available_at=d1_end,
                    corporate_action_adjustment_version="unadjusted.v1",
                    quality_flags=(),
                )
            )
            _contracts_and_quotes(store, symbol, day, open_at)
    store.publish()
    return store


def _next_tuesday(day: date) -> date:
    probe = day + timedelta(days=1)
    while probe.weekday() != 1:
        probe += timedelta(days=1)
    return probe


def _contracts_and_quotes(store: CanonicalStore, symbol: str, day: date, open_at: datetime) -> None:
    expiry = _next_tuesday(day)
    step = 50.0 if symbol == "NIFTY" else 100.0
    atm = 25000.0 if symbol == "NIFTY" else 52000.0
    lot = 75 if symbol == "NIFTY" else 15
    first = open_at
    last = datetime.combine(expiry, time(15, 30), tzinfo=IST)
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
                        expiry_class="WEEKLY",
                        first_seen_at=first,
                        last_seen_at=last,
                        listing_status="ACTIVE",
                        source_id=SOURCE,
                        dataset_version=SAMPLE_VERSION,
                    )
                )
            for hh, mm in ((11, 0), (15, 15)):
                ts = datetime.combine(day, time(hh, mm), tzinfo=IST)
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
                        delta=0.5 if kind == "CE" else -0.5,
                        gamma=None,
                        theta=None,
                        vega=None,
                        greek_source="PROVIDER",
                        iv_source="PROVIDER",
                        source_id=SOURCE,
                        dataset_version=SAMPLE_VERSION,
                        as_of_available_at=ts,
                        quality_flags=(),
                    )
                )
