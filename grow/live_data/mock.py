"""Deterministic in-process stream. Not a vendor feed. Not licensed market data."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping
from uuid import uuid4

from grow.clock import IST
from grow.data.schedule import bar_duration, expected_starts
from grow.data.schema import Timeframe
from grow.errors import GrowConfigError
from grow.live_data.models import ADAPTER_VERSION, MOCK_PROVIDER_ID, LiveHealth, SessionHealth
from grow.live_data.provider import _FORBIDDEN_FALLBACK

AS_OF = datetime(2026, 9, 18, 11, 0, tzinfo=IST)
WEEKLY = date(2026, 9, 22)
MONTHLY = date(2026, 9, 24)
OPEN_T = datetime(2026, 9, 18, 9, 15, tzinfo=IST).timetz().replace(tzinfo=None)


class MockStreamProvider:
    identity = MOCK_PROVIDER_ID
    adapter_version = ADAPTER_VERSION

    def __init__(self, events: tuple[Mapping[str, Any], ...] | None = None) -> None:
        self._events = list(events if events is not None else (bullish_event(),))
        self._index = 0
        self._state = SessionHealth.DISCONNECTED
        self._last_at: datetime | None = None
        self._last_seq: int | None = None
        self._error: str | None = None

    def connect(self) -> None:
        self._state = SessionHealth.CONNECTING
        self._state = SessionHealth.READY
        self._error = None

    def disconnect(self) -> None:
        self._state = SessionHealth.STOPPED

    def health(self) -> LiveHealth:
        return LiveHealth(
            state=self._state,
            provider_id=self.identity,
            adapter_version=self.adapter_version,
            last_message_at=self._last_at,
            last_sequence=self._last_seq,
            error=self._error,
        )

    def poll(self) -> dict[str, Any] | None:
        if self._state in {SessionHealth.DISCONNECTED, SessionHealth.STOPPED, SessionHealth.CONNECTING}:
            return None
        if self._index >= len(self._events):
            return None
        raw = dict(self._events[self._index])
        self._index += 1
        provider = str(raw.get("provider") or self.identity)
        if provider in _FORBIDDEN_FALLBACK or raw.get("is_fixture") is True:
            self._state = SessionHealth.DEGRADED
            self._error = "FIXTURE_FALLBACK_FORBIDDEN"
            raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
        if provider != MOCK_PROVIDER_ID:
            self._state = SessionHealth.DEGRADED
            self._error = f"PROVIDER_NOT_APPROVED:{provider}"
            raise GrowConfigError(f"PROVIDER_NOT_APPROVED:{provider}")
        self._last_seq = int(raw.get("sequence") or 0)
        stamp = raw.get("received_time") or raw.get("event_time")
        if isinstance(stamp, str):
            try:
                self._last_at = datetime.fromisoformat(stamp)
            except ValueError:
                self._last_at = None
        elif isinstance(stamp, datetime):
            self._last_at = stamp
        if self._state is SessionHealth.READY:
            self._state = SessionHealth.RUNNING
        return raw


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _tradingsymbol(underlying: str, expiry: date, strike: float, kind: str) -> str:
    return f"{underlying}{expiry.strftime('%d%b%y').upper()}{int(strike)}{kind}"


def _session_days(end: date, count: int) -> list[date]:
    days: list[date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    return days


def _bars(symbol: str, as_of: datetime, *, start_px: float, step: float, n: int = 80) -> list[dict[str, Any]]:
    days = _session_days(as_of.date(), 8)
    rows: list[dict[str, Any]] = []
    px = start_px
    tf = Timeframe.M15
    dur = bar_duration(tf)
    from datetime import time as time_t

    for day in days:
        for start in expected_starts(day, tf, session_open=time_t(9, 15), session_close=time_t(15, 30)):
            end = start + dur
            if end > as_of:
                continue
            rows.append(
                {
                    "underlying": symbol,
                    "timeframe": "M15",
                    "start": _iso(start),
                    "end": _iso(end),
                    "open": round(px - 0.4, 2),
                    "high": round(px + 0.8, 2),
                    "low": round(px - 0.8, 2),
                    "close": round(px, 2),
                    "volume": 1000,
                }
            )
            px += step
    d1_px = start_px
    for day in _session_days(as_of.date(), 60):
        open_at = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
        close_at = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
        if close_at > as_of and day == as_of.date():
            close_at = as_of
        rows.append(
            {
                "underlying": symbol,
                "timeframe": "D1",
                "start": _iso(open_at),
                "end": _iso(close_at),
                "open": round(d1_px - 2, 2),
                "high": round(d1_px + 20, 2),
                "low": round(d1_px - 20, 2),
                "close": round(d1_px, 2),
                "volume": 100000,
            }
        )
        d1_px += step * 3
    return rows[-n - 60 :]


def _contracts(symbol: str, spot: float, as_of: datetime, *, incomplete: bool = False, missing_lot: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    step = 50.0 if symbol == "NIFTY" else 100.0 if symbol == "BANKNIFTY" else 25.0
    lot = None if missing_lot else (75 if symbol != "BANKNIFTY" else 15)
    atm = round(spot / step) * step
    specs = [(WEEKLY, "WEEKLY")]
    if symbol == "MIDCPNIFTY":
        specs = [(MONTHLY, "MONTHLY")]
    else:
        specs.append((MONTHLY, "MONTHLY"))
    master: list[dict[str, Any]] = []
    quotes: list[dict[str, Any]] = []
    for expiry, klass in specs:
        for k in range(-2, 3):
            strike = atm + k * step
            for kind in ("CE", "PE"):
                ts_sym = _tradingsymbol(symbol, expiry, strike, kind)
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
                    }
                )
                if incomplete:
                    continue
                prem = 80.0 + abs(k) * 10
                quotes.append(
                    {
                        "underlying": symbol,
                        "expiry": expiry.isoformat(),
                        "strike": strike,
                        "option_type": kind,
                        "ts": _iso(as_of),
                        "bid": prem - 0.5,
                        "ask": prem + 0.5,
                        "ltp": prem,
                        "volume": 200,
                        "oi": 5000,
                    }
                )
    return master, quotes


def stream_event(
    *,
    as_of: datetime = AS_OF,
    sequence: int = 1,
    trend: str = "up",
    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY", "MIDCPNIFTY"),
    incomplete: bool = False,
    missing_lot: bool = False,
    naive: bool = False,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    bars: list[dict[str, Any]] = []
    master: list[dict[str, Any]] = []
    quotes: list[dict[str, Any]] = []
    spots: dict[str, float] = {}
    for symbol in underlyings:
        base = {"NIFTY": 25000.0, "BANKNIFTY": 52000.0, "MIDCPNIFTY": 13000.0}[symbol]
        step = 8.0 if trend == "up" else -8.0
        rows = _bars(symbol, as_of, start_px=base, step=step)
        bars.extend(rows)
        m15 = [r for r in rows if r["timeframe"] == "M15"]
        spot = float(m15[-1]["close"]) if m15 else base
        spots[symbol] = spot
        m, q = _contracts(symbol, spot, as_of, incomplete=incomplete, missing_lot=missing_lot)
        master.extend(m)
        quotes.extend(q)
    event_time = as_of.replace(tzinfo=None).isoformat() if naive else _iso(as_of)
    payload = {
        "provider": MOCK_PROVIDER_ID,
        "adapter_version": ADAPTER_VERSION,
        "schema": "grow.stream.snapshot.v1",
        "sequence": sequence,
        "event_time": event_time,
        "received_time": _iso(as_of),
        "session_date": as_of.date().isoformat(),
        "source_timezone": "Asia/Kolkata",
        "instrument_type": "OPTIDX",
        "underlyings": list(underlyings),
        "spots": spots,
        "spot_bars": bars,
        "contract_master": master,
        "option_quotes": quotes,
        "is_fixture": False,
        "snapshot_id": f"stream-{sequence}-{uuid4().hex[:8]}",
    }
    if extra:
        payload.update(dict(extra))
    return payload


def bullish_event(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("trend", "up")
    return stream_event(**kwargs)


def bearish_event(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("trend", "down")
    return stream_event(**kwargs)
