"""Recorded NSE F&O-shaped payload for 2J tests. Not a licensed vendor feed."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from grow.clock import IST
from grow.data.schedule import bar_duration, expected_starts
from grow.data.schema import Timeframe
from grow.history.integrate.contract import RECORDED_PROVIDER_ID, VENDOR_SCHEMA, AcquireScope

START = date(2026, 9, 14)
END = date(2026, 9, 18)
WEEKLY_SAME = date(2026, 9, 15)
WEEKLY_NEXT = date(2026, 9, 22)
WEEKLY_LATE = date(2026, 9, 29)
MONTHLY = date(2026, 9, 24)
MIDCP_MONTHLY = date(2026, 9, 29)
OPEN_T = time(9, 15)
CLOSE_T = time(15, 30)
DATASET_ID = "grow.history.recorded.nse_fo.v1"
DATASET_VERSION = "2026.09.2j.a"
UNDERLYINGS = ("NIFTY", "BANKNIFTY", "MIDCPNIFTY")


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _symbol_px(symbol: str, start: datetime) -> float:
    base = {"NIFTY": 25000.0, "BANKNIFTY": 52000.0, "MIDCPNIFTY": 13000.0}[symbol]
    return round(base + start.hour * 0.5 + start.minute * 0.01 + start.date().day, 2)


def _tradingsymbol(underlying: str, expiry: date, strike: float, kind: str) -> str:
    return f"{underlying}{expiry.strftime('%d%b%y').upper()}{int(strike)}{kind}"


def recorded_scope() -> AcquireScope:
    return AcquireScope(underlyings=UNDERLYINGS, start=START, end=END, provider_id=RECORDED_PROVIDER_ID)


def recorded_payload(*, incomplete: bool = False, naive: bool = False, extra_future: bool = False) -> dict:
    calendar = []
    bars = []
    master = []
    quotes = []
    seen_contracts: set[str] = set()
    day = START
    while day <= END:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        open_at = datetime.combine(day, OPEN_T, tzinfo=IST)
        close_at = datetime.combine(day, CLOSE_T, tzinfo=IST)
        calendar.append(
            {
                "date": day.isoformat(),
                "open": _iso(open_at),
                "close": _iso(close_at),
                "status": "OPEN",
            }
        )
        for symbol in UNDERLYINGS:
            tf = Timeframe.M15
            dur = bar_duration(tf)
            for start in expected_starts(day, tf, session_open=OPEN_T, session_close=CLOSE_T):
                end = start + dur
                px = _symbol_px(symbol, start)
                bars.append(
                    {
                        "underlying": symbol,
                        "timeframe": "M15",
                        "start": _iso(start) if not naive else start.replace(tzinfo=None).isoformat(),
                        "end": _iso(end),
                        "open": px,
                        "high": px + 2,
                        "low": px - 2,
                        "close": px + 0.5,
                        "volume": 1000,
                    }
                )
            px = _symbol_px(symbol, open_at)
            bars.append(
                {
                    "underlying": symbol,
                    "timeframe": "D1",
                    "start": _iso(open_at),
                    "end": _iso(close_at),
                    "open": px,
                    "high": px + 20,
                    "low": px - 20,
                    "close": px + 5,
                    "volume": 100000,
                }
            )
            _append_contracts(master, quotes, seen_contracts, symbol, day, open_at, incomplete=incomplete)
        day += timedelta(days=1)
    if extra_future:
        future_day = date(2026, 10, 6)
        master.append(
            {
                "tradingsymbol": _tradingsymbol("NIFTY", future_day, 25000, "CE"),
                "underlying": "NIFTY",
                "expiry": future_day.isoformat(),
                "strike": 25000,
                "option_type": "CE",
                "instrument_type": "OPTIDX",
                "lot_size": 65,
                "expiry_class": "WEEKLY",
                "listed_from": _iso(datetime(2026, 9, 21, 9, 15, tzinfo=IST)),
                "listed_to": _iso(datetime(2026, 10, 6, 15, 30, tzinfo=IST)),
            }
        )
        quotes.append(
            {
                "underlying": "NIFTY",
                "expiry": future_day.isoformat(),
                "strike": 25000,
                "option_type": "CE",
                "ts": _iso(datetime(2026, 9, 21, 11, 0, tzinfo=IST)),
                "bid": 90.0,
                "ask": 91.0,
                "ltp": 90.5,
                "volume": 200,
                "oi": 5000,
                "poi": 4800,
            }
        )
    return {
        "provider": RECORDED_PROVIDER_ID,
        "vendor_schema": VENDOR_SCHEMA,
        "instrument_type": "OPTIDX",
        "source_timezone": "Asia/Kolkata",
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "calendar_version": "nse.session.recorded.v1",
        "usage_scope": "HISTORICAL_RESEARCH",
        "license_status": "APPROVED",
        "quality_status": "APPROVED",
        "iv_available": False,
        "greeks_available": False,
        "underlyings": list(UNDERLYINGS),
        "granularity": ["M15", "D1"],
        "snapshot_cadence": ["11:00", "15:15"],
        "coverage": {"start": START.isoformat(), "end": END.isoformat()},
        "retrieved_at": "2026-09-18T16:00:00+05:30",
        "calendar": calendar,
        "spot_bars": bars,
        "contract_master": master,
        "option_quotes": quotes,
    }


def _append_contracts(master, quotes, seen, symbol, day, open_at, *, incomplete: bool) -> None:
    if symbol == "MIDCPNIFTY":
        specs = (
            (MIDCP_MONTHLY, "MONTHLY", datetime(2026, 9, 14, 9, 15, tzinfo=IST), datetime(2026, 9, 29, 15, 30, tzinfo=IST), 13000.0, 25.0, 75),
        )
    else:
        atm = 25000.0 if symbol == "NIFTY" else 52000.0
        step = 50.0 if symbol == "NIFTY" else 100.0
        lot = 75 if symbol == "NIFTY" else 15
        specs = (
            (WEEKLY_SAME, "WEEKLY", datetime(2026, 9, 14, 9, 15, tzinfo=IST), datetime(2026, 9, 15, 15, 30, tzinfo=IST), atm, step, lot),
            (WEEKLY_NEXT, "WEEKLY", datetime(2026, 9, 14, 9, 15, tzinfo=IST), datetime(2026, 9, 22, 15, 30, tzinfo=IST), atm, step, lot),
            (WEEKLY_LATE, "WEEKLY", datetime(2026, 9, 16, 9, 15, tzinfo=IST), datetime(2026, 9, 29, 15, 30, tzinfo=IST), atm, step, lot),
            (MONTHLY, "MONTHLY", datetime(2026, 9, 14, 9, 15, tzinfo=IST), datetime(2026, 9, 24, 15, 30, tzinfo=IST), atm, step, lot),
        )
    for expiry, klass, first, last, atm, step, lot in specs:
        if open_at < first or open_at > last:
            continue
        for k in range(-2, 3):
            strike = atm + k * step
            for kind in ("CE", "PE"):
                ts_sym = _tradingsymbol(symbol, expiry, strike, kind)
                key = f"{symbol}-{expiry.isoformat()}-{int(strike)}-{kind}"
                if key not in seen:
                    seen.add(key)
                    master.append(
                        {
                            "tradingsymbol": ts_sym,
                            "underlying": symbol,
                            "expiry": expiry.isoformat(),
                            "strike": strike,
                            "option_type": kind,
                            "instrument_type": "OPTIDX",
                            "lot_size": lot,
                            "expiry_class": klass,
                            "listed_from": _iso(first),
                            "listed_to": _iso(last),
                        }
                    )
                if incomplete:
                    continue
                for hh, mm in ((11, 0), (15, 15)):
                    ts = datetime.combine(day, time(hh, mm), tzinfo=IST)
                    if not (first <= ts <= last):
                        continue
                    prem = 80.0 + abs(k) * 10
                    quotes.append(
                        {
                            "underlying": symbol,
                            "expiry": expiry.isoformat(),
                            "strike": strike,
                            "option_type": kind,
                            "ts": _iso(ts),
                            "bid": prem - 0.5,
                            "ask": prem + 0.5,
                            "ltp": prem,
                            "volume": 200,
                            "oi": 5000,
                            "poi": 4800,
                        }
                    )
