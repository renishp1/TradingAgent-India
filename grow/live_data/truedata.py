"""TrueData live-market-data adapter. Paper quotes only. No broker. No orders."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence
from uuid import uuid4

from grow.clock import IST, Clock, FrozenClock
from grow.errors import GrowConfigError
from grow.history.resolver import resolve_nearest_expiry
from grow.history.models import HistoricalExpiryRecord
from grow.history.universe import IndexUniverseRegistry, default_index_registry, is_forbidden_instrument
from grow.live_data.models import (
    TRUEDATA_PROVIDER_ID,
    LiveHealth,
    SessionHealth,
)
from grow.live_data.provider import _FORBIDDEN_FALLBACK
from grow.live_data.subscribe import plan_subscriptions
from grow.live_data.symbols import ParsedInstrument, parse_provider_symbol, parse_tick_fields

TRUEDATA_ADAPTER_VERSION = "live_data.truedata.v1"


def load_truedata_secrets(
    environ: Mapping[str, str] | None = None,
    *,
    username_env: str = "TRUEDATA_USERNAME",
    password_env: str = "TRUEDATA_PASSWORD",
) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    user = (env.get(username_env) or "").strip()
    password = (env.get(password_env) or "").strip()
    if not user or not password:
        raise GrowConfigError("AUTH_MISSING")
    return user, password


class ReplayTransport:
    """Captured vendor messages. Not a licensed live socket. Not a 2E dataset."""

    def __init__(
        self,
        events: Sequence[Mapping[str, Any]] | None = None,
        *,
        login_ok: bool = True,
        catalog: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.events = [dict(item) for item in (events or ())]
        self.login_ok = login_ok
        self.catalog_rows = [dict(item) for item in (catalog or ())]
        self.connected = False
        self.sent: list[str] = []
        self.subscribed: list[str] = []
        self._index = 0
        self.reconnects = 0
        self.fail_next = False

    def connect(self) -> None:
        if not self.login_ok:
            raise GrowConfigError("AUTH_FAILED")
        self.connected = True
        self._index = 0

    def disconnect(self) -> None:
        self.connected = False

    def subscribe(self, symbols: Sequence[str]) -> None:
        if not self.connected:
            raise GrowConfigError("FEED_DISCONNECTED")
        self.sent.append("subscribe:" + ",".join(symbols))
        for symbol in symbols:
            if symbol not in self.subscribed:
                self.subscribed.append(symbol)

    def unsubscribe(self, symbols: Sequence[str]) -> None:
        drop = set(symbols)
        self.subscribed = [s for s in self.subscribed if s not in drop]
        self.sent.append("unsubscribe:" + ",".join(symbols))

    def recv(self) -> Mapping[str, Any] | None:
        if not self.connected:
            raise GrowConfigError("FEED_DISCONNECTED")
        if getattr(self, "fail_next", False):
            self.fail_next = False
            self.connected = False
            raise GrowConfigError("FEED_DISCONNECTED")
        if self._index >= len(self.events):
            return None
        payload = dict(self.events[self._index])
        self._index += 1
        return payload

    def resubscribe(self, symbols: Sequence[str]) -> None:
        self.reconnects += 1
        self.connected = True
        self.subscribed = []
        self.subscribe(symbols)


class RealTrueDataTransport:
    """Optional websocket-client transport. Missing library or credentials fail closed."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        endpoint: str,
        port: int,
        bid_ask: bool = True,
    ) -> None:
        if not username or not password:
            raise GrowConfigError("AUTH_MISSING")
        self.username = username
        self.password = password
        self.endpoint = endpoint
        self.port = port
        self.bid_ask = bid_ask
        self.connected = False
        self.subscribed: list[str] = []
        self._ws = None

    def connect(self) -> None:
        try:
            import importlib

            websocket = importlib.import_module("websocket")
        except ImportError as exc:
            raise GrowConfigError("TRANSPORT_UNAVAILABLE:websocket-client") from exc
        url = self.endpoint
        if "://" not in url:
            url = f"wss://{url}:{self.port}"
        try:
            self._ws = websocket.create_connection(url, timeout=10)
            self._ws.send(f"{self.username}:{self.password}")
            reply = str(self._ws.recv() or "")
        except GrowConfigError:
            raise
        except Exception as exc:
            raise GrowConfigError("AUTH_FAILED") from exc
        if "fail" in reply.lower() or "invalid" in reply.lower() or "unauthorized" in reply.lower():
            self.disconnect()
            raise GrowConfigError("AUTH_FAILED")
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        socket = self._ws
        self._ws = None
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    def subscribe(self, symbols: Sequence[str]) -> None:
        if not self.connected or self._ws is None:
            raise GrowConfigError("FEED_DISCONNECTED")
        payload = "addsymbol:" + "+".join(symbols)
        self._ws.send(payload)
        self.subscribed = list(dict.fromkeys([*self.subscribed, *symbols]))

    def unsubscribe(self, symbols: Sequence[str]) -> None:
        if self._ws is not None and self.connected:
            self._ws.send("unsymbol:" + "+".join(symbols))
        drop = set(symbols)
        self.subscribed = [s for s in self.subscribed if s not in drop]

    def recv(self) -> Mapping[str, Any] | None:
        if not self.connected or self._ws is None:
            raise GrowConfigError("FEED_DISCONNECTED")
        try:
            raw = self._ws.recv()
        except Exception as exc:
            raise GrowConfigError("FEED_DISCONNECTED") from exc
        if raw is None or raw == "":
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return {"kind": "tick_csv", "line": str(raw)}

    def resubscribe(self, symbols: Sequence[str]) -> None:
        self.connect()
        self.subscribed = []
        self.subscribe(symbols)


@dataclass
class TrueDataSettings:
    username_env: str = "TRUEDATA_USERNAME"
    password_env: str = "TRUEDATA_PASSWORD"
    endpoint: str = "push.truedata.in"
    port: int = 8082
    bid_ask: bool = True
    max_symbols: int = 50
    strike_window: int = 4
    reconnect_policy: str = "bounded_backoff"
    max_attempts: int = 5
    max_backoff_seconds: int = 30
    mode: str = "real"


class TrueDataAdapter:
    """Vendor adapter. Downstream code never sees TrueData-native objects."""

    identity = TRUEDATA_PROVIDER_ID
    adapter_version = TRUEDATA_ADAPTER_VERSION

    def __init__(
        self,
        *,
        events: Sequence[Mapping[str, Any]] | None = None,
        catalog: Sequence[Mapping[str, Any]] | None = None,
        transport: ReplayTransport | RealTrueDataTransport | None = None,
        settings: TrueDataSettings | None = None,
        clock: Clock | None = None,
        registry: IndexUniverseRegistry | None = None,
        environ: Mapping[str, str] | None = None,
        as_of: datetime | None = None,
    ) -> None:
        self.settings = settings or TrueDataSettings()
        self.clock = clock or FrozenClock(datetime.now(tz=IST))
        self.registry = registry or default_index_registry()
        self._as_of = as_of
        self._state = SessionHealth.DISCONNECTED
        self._error: str | None = None
        self._last_at: datetime | None = None
        self._last_seq: int | None = None
        self._adapter_seq = 0
        self.reconnect_count = 0
        self.connection_events: list[dict[str, Any]] = []
        self.subscription_events: list[dict[str, Any]] = []
        self._quotes: dict[str, dict[str, Any]] = {}
        self._spots: dict[str, float] = {}
        self._bars: list[dict[str, Any]] = []
        self._symbol_seq: dict[str, int] = {}
        self._desired: tuple[str, ...] = ()
        self._retry_at: datetime | None = None
        self._attempts = 0
        self._snapshot_queue: list[dict[str, Any]] = []
        replay = events is not None or transport is not None or self.settings.mode == "replay"
        if transport is not None:
            self.transport = transport
        elif replay:
            self.transport = ReplayTransport(events or (), catalog=catalog)
        else:
            user, password = load_truedata_secrets(
                environ,
                username_env=self.settings.username_env,
                password_env=self.settings.password_env,
            )
            self.transport = RealTrueDataTransport(
                username=user,
                password=password,
                endpoint=self.settings.endpoint,
                port=self.settings.port,
                bid_ask=self.settings.bid_ask,
            )
        self._instruments: list[dict[str, Any]] = [dict(row) for row in (catalog or ())]
        if not self._instruments and isinstance(self.transport, ReplayTransport):
            self._instruments = [dict(row) for row in self.transport.catalog_rows]
        seed_events: Sequence[Mapping[str, Any]] = events or ()
        if not seed_events and isinstance(self.transport, ReplayTransport):
            seed_events = self.transport.events
        self._seed_catalog_from_events(seed_events)

    def _seed_catalog_from_events(self, events: Sequence[Mapping[str, Any]]) -> None:
        for event in events:
            if event.get("kind") == "catalog":
                self._instruments.extend(dict(row) for row in (event.get("instruments") or ()))
            master = event.get("contract_master")
            if master:
                for row in master:
                    self._ingest_master_row(row)

    def _ingest_master_row(self, row: Mapping[str, Any]) -> None:
        provider_symbol = str(row.get("tradingsymbol") or row.get("provider_symbol") or row.get("provider_contract_id") or "")
        if not provider_symbol:
            return
        parsed = parse_provider_symbol(provider_symbol)
        expiry = parsed.expiry
        if row.get("expiry"):
            expiry = date.fromisoformat(str(row["expiry"]))
        record = {
            "provider_symbol": provider_symbol,
            "canonical_symbol": str(row.get("underlying") or parsed.canonical_symbol).upper(),
            "instrument_type": "INDEX_OPTION" if parsed.option_type else "INDEX",
            "expiry": expiry,
            "strike": parsed.strike if row.get("strike") is None else float(row["strike"]),
            "option_type": parsed.option_type if row.get("option_type") is None else str(row["option_type"]).upper(),
            "lot_size": None if row.get("lot_size") is None else int(row["lot_size"]),
            "expiry_class": str(row.get("expiry_class") or "WEEKLY").upper(),
        }
        key = record["provider_symbol"]
        existing = {item["provider_symbol"]: item for item in self._instruments}
        existing[key] = record
        self._instruments = list(existing.values())

    def connect(self) -> None:
        self._state = SessionHealth.CONNECTING
        self._note("CONNECTING", "connect")
        try:
            self.transport.connect()
        except GrowConfigError as exc:
            self._error = str(exc)
            self._state = SessionHealth.STOPPED
            self._note("STOPPED", str(exc))
            raise
        self._error = None
        self._state = SessionHealth.READY
        self._note("READY", "authenticated")
        if self._instruments:
            self._refresh_plan()

    def disconnect(self) -> None:
        self.transport.disconnect()
        self._state = SessionHealth.STOPPED
        self._note("STOPPED", "disconnect")

    def health(self) -> LiveHealth:
        return LiveHealth(
            state=self._state,
            provider_id=self.identity,
            adapter_version=self.adapter_version,
            last_message_at=self._last_at,
            last_sequence=self._last_seq,
            error=self._error,
            reconnect_count=self.reconnect_count,
            subscribed=self._desired,
        )

    def instrument_catalog(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(row) for row in self._instruments)

    def market_status(self) -> dict[str, Any]:
        return {
            "provider": self.identity,
            "state": self._state.value,
            "reconnect_count": self.reconnect_count,
            "subscribed": list(self._desired),
            "live_trading": False,
        }

    def discover_underlyings(self, day: date | None = None) -> tuple[str, ...]:
        found: list[str] = []
        for row in self._instruments:
            symbol = str(row["canonical_symbol"]).upper()
            if is_forbidden_instrument(symbol):
                continue
            if symbol in found:
                continue
            found.append(symbol)
        day = day or self.clock.now().date()
        allowed = []
        ignored = []
        for symbol in found:
            if self.registry.allows(symbol, day):
                allowed.append(symbol)
            else:
                ignored.append(symbol)
        self.subscription_events.append(
            {
                "type": "UNIVERSE",
                "discovered": found,
                "allowed": allowed,
                "ignored": ignored,
                "policy_fingerprint": self.registry.fingerprint,
                "timestamp": self.clock.now().isoformat(),
            }
        )
        return tuple(allowed)

    def subscribe(self, symbols: Sequence[str]) -> None:
        planned = tuple(symbols)
        if len(planned) > self.settings.max_symbols:
            raise GrowConfigError("SUBSCRIPTION_LIMIT")
        self.transport.subscribe(planned)
        self._desired = tuple(dict.fromkeys([*self._desired, *planned]))
        self.subscription_events.append(
            {
                "type": "SUBSCRIBE",
                "symbols": list(planned),
                "count": len(self._desired),
                "timestamp": self.clock.now().isoformat(),
            }
        )

    def unsubscribe(self, symbols: Sequence[str]) -> None:
        self.transport.unsubscribe(symbols)
        drop = set(symbols)
        self._desired = tuple(s for s in self._desired if s not in drop)

    def poll(self) -> dict[str, Any] | None:
        if self._state in {SessionHealth.DISCONNECTED, SessionHealth.STOPPED, SessionHealth.CONNECTING}:
            return None
        if self._state is SessionHealth.DEGRADED and self.settings.reconnect_policy == "bounded_backoff":
            return self._reconnect_poll()
        try:
            raw = self.transport.recv()
        except GrowConfigError as exc:
            return self._on_feed_error(str(exc))
        if raw is None:
            if self._snapshot_queue:
                return self._snapshot_queue.pop(0)
            return None
        provider = str(raw.get("provider") or self.identity)
        if provider in _FORBIDDEN_FALLBACK or raw.get("is_fixture") is True:
            self._state = SessionHealth.DEGRADED
            self._error = "FIXTURE_FALLBACK_FORBIDDEN"
            raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
        if raw.get("kind") == "auth" and raw.get("ok") is False:
            self._state = SessionHealth.STOPPED
            self._error = "AUTH_FAILED"
            raise GrowConfigError("AUTH_FAILED")
        if raw.get("kind") == "catalog":
            self._instruments.extend(dict(row) for row in (raw.get("instruments") or ()))
            self._refresh_plan()
            return self.poll()
        if raw.get("kind") in {"tick", "tick_csv"} or (
            raw.get("symbol") and not raw.get("schema") and "contract_master" not in raw
        ):
            self._ingest_tick(raw)
            return self._assemble_snapshot()
        if raw.get("schema") == "grow.stream.snapshot.v1" or raw.get("contract_master") is not None:
            payload = dict(raw)
            payload["provider"] = self.identity
            payload["adapter_version"] = self.adapter_version
            payload["is_fixture"] = False
            try:
                seq = int(payload.get("sequence") or 0)
            except (TypeError, ValueError) as exc:
                raise GrowConfigError("INVALID_SEQUENCE") from exc
            if seq < 1:
                raise GrowConfigError("INVALID_SEQUENCE")
            if self._last_seq is not None and seq == self._last_seq:
                raise GrowConfigError("DUPLICATE_SEQUENCE")
            if self._last_seq is not None and seq < self._last_seq:
                raise GrowConfigError("OUT_OF_ORDER")
            self._adapter_seq = seq
            self._last_seq = seq
            stamp = payload.get("received_time") or payload.get("event_time")
            if isinstance(stamp, str):
                try:
                    self._last_at = datetime.fromisoformat(stamp)
                except ValueError:
                    self._last_at = self.clock.now()
            self._seed_catalog_from_events((payload,))
            if self._state is SessionHealth.READY:
                self._state = SessionHealth.RUNNING
            return payload
        self._ingest_tick(raw)
        return self._assemble_snapshot()

    def _ingest_tick(self, raw: Mapping[str, Any]) -> None:
        if raw.get("kind") == "tick_csv":
            fields = parse_tick_fields(str(raw.get("line") or ""))
        else:
            fields = parse_tick_fields(raw)
        symbol = str(fields.get("provider_symbol") or "")
        if not symbol:
            raise GrowConfigError("UNKNOWN_INSTRUMENT")
        parsed = parse_provider_symbol(symbol)
        seq = fields.get("sequence")
        if seq is not None:
            last = self._symbol_seq.get(symbol)
            if last is not None and int(seq) < last:
                raise GrowConfigError("OUT_OF_ORDER")
            if last is not None and int(seq) == last:
                raise GrowConfigError("DUPLICATE_SEQUENCE")
            self._symbol_seq[symbol] = int(seq)
        quote = {
            "underlying": parsed.canonical_symbol,
            "expiry": None if parsed.expiry is None else parsed.expiry.isoformat(),
            "strike": parsed.strike,
            "option_type": parsed.option_type,
            "ts": fields.get("timestamp"),
            "bid": fields.get("bid"),
            "ask": fields.get("ask"),
            "ltp": fields.get("ltp"),
            "volume": fields.get("volume"),
            "oi": fields.get("oi"),
            "provider_symbol": symbol,
            "iv": fields.get("iv"),
            "delta": fields.get("delta"),
            "gamma": fields.get("gamma"),
            "theta": fields.get("theta"),
            "vega": fields.get("vega"),
        }
        stamp = fields.get("timestamp")
        if stamp in (None, ""):
            raise GrowConfigError("MISSING_TIMESTAMP:event_time")
        if isinstance(stamp, datetime):
            event_time = stamp
        else:
            try:
                event_time = datetime.fromisoformat(str(stamp))
            except ValueError as exc:
                raise GrowConfigError("INVALID_TIMESTAMP:event_time") from exc
        if event_time.tzinfo is None:
            raise GrowConfigError("NAIVE_TIMESTAMP:event_time")
        self._last_at = event_time.astimezone(IST)
        if parsed.instrument_type == "INDEX":
            if fields.get("ltp") is None:
                raise GrowConfigError(f"MISSING_SPOT:{parsed.canonical_symbol}")
            self._spots[parsed.canonical_symbol] = float(fields["ltp"])
        else:
            self._quotes[symbol] = quote

    def _assemble_snapshot(self) -> dict[str, Any] | None:
        underlyings = self.discover_underlyings()
        if not underlyings or not self._spots:
            return None
        self._adapter_seq += 1
        event_time = self._last_at or self.clock.now()
        if isinstance(event_time, datetime) and event_time.tzinfo is None:
            raise GrowConfigError("NAIVE_TIMESTAMP:event_time")
        master = []
        quotes = []
        for row in self._instruments:
            if row["canonical_symbol"] not in underlyings:
                continue
            if row.get("option_type") in {"CE", "PE"}:
                master.append(
                    {
                        "tradingsymbol": row["provider_symbol"],
                        "underlying": row["canonical_symbol"],
                        "expiry": None if row["expiry"] is None else row["expiry"].isoformat(),
                        "strike": row["strike"],
                        "option_type": row["option_type"],
                        "instrument_type": "OPTIDX",
                        "lot_size": row.get("lot_size"),
                        "expiry_class": row.get("expiry_class") or "WEEKLY",
                    }
                )
                quote = self._quotes.get(row["provider_symbol"])
                if quote:
                    quotes.append(quote)
        payload = {
            "provider": self.identity,
            "adapter_version": self.adapter_version,
            "schema": "grow.stream.snapshot.v1",
            "sequence": self._adapter_seq,
            "event_time": event_time.isoformat() if isinstance(event_time, datetime) else event_time,
            "received_time": self.clock.now().isoformat(),
            "session_date": self.clock.now().date().isoformat(),
            "source_timezone": "Asia/Kolkata",
            "instrument_type": "OPTIDX",
            "underlyings": list(underlyings),
            "spots": dict(self._spots),
            "spot_bars": list(self._bars),
            "contract_master": master,
            "option_quotes": quotes,
            "is_fixture": False,
            "snapshot_id": f"td-{self._adapter_seq}-{uuid4().hex[:8]}",
        }
        if self._state is SessionHealth.READY:
            self._state = SessionHealth.RUNNING
        self._last_seq = self._adapter_seq
        return payload

    def _refresh_plan(self) -> None:
        allowed = self.discover_underlyings()
        spots = dict(self._spots)
        for symbol in allowed:
            spots.setdefault(symbol, 0.0)
        selected: dict[str, date] = {}
        parsed = []
        for row in self._instruments:
            expiry = row.get("expiry")
            parsed.append(
                ParsedInstrument(
                    provider_symbol=str(row["provider_symbol"]),
                    canonical_symbol=str(row["canonical_symbol"]),
                    instrument_type=str(row.get("instrument_type") or "INDEX_OPTION"),
                    expiry=expiry if isinstance(expiry, date) else (date.fromisoformat(str(expiry)) if expiry else None),
                    strike=None if row.get("strike") is None else float(row["strike"]),
                    option_type=None if row.get("option_type") is None else str(row["option_type"]),
                )
            )
            if row.get("option_type") in {"CE", "PE"} and row.get("expiry") is not None:
                continue
        as_of = self.clock.now()
        for symbol in allowed:
            records = self._expiry_records(symbol)
            policy = self.registry.policy(symbol, as_of.date())
            profile = policy.expiry_policy_profile if policy is not None else "WEEKLY_PREFERRED"
            resolution = resolve_nearest_expiry(symbol, as_of, records, profile, allow_same_day=False)
            if resolution.selected_expiry:
                selected[symbol] = date.fromisoformat(resolution.selected_expiry)
        planned = plan_subscriptions(
            instruments=parsed,
            spots=spots,
            selected_expiry=selected,
            strike_window=self.settings.strike_window,
            max_symbols=self.settings.max_symbols,
            active_underlyings=allowed,
        )
        if planned:
            self.transport.subscribe(planned)
            self._desired = planned
            self.subscription_events.append(
                {
                    "type": "PLAN",
                    "symbols": list(planned),
                    "policy_fingerprint": self.registry.fingerprint,
                    "timestamp": as_of.isoformat(),
                }
            )

    def _expiry_records(self, symbol: str) -> tuple[HistoricalExpiryRecord, ...]:
        grouped: dict[tuple[date, str], list[datetime]] = {}
        as_of = self.clock.now()
        for row in self._instruments:
            if row["canonical_symbol"] != symbol or row.get("expiry") is None:
                continue
            expiry = row["expiry"] if isinstance(row["expiry"], date) else date.fromisoformat(str(row["expiry"]))
            klass = str(row.get("expiry_class") or "WEEKLY").upper()
            grouped.setdefault((expiry, klass), []).append(as_of)
        rows = []
        for (expiry, klass), _times in grouped.items():
            rows.append(
                HistoricalExpiryRecord(
                    underlying=symbol,
                    expiry=expiry,
                    expiry_class=klass,
                    first_seen_at=as_of - timedelta(days=14),
                    last_seen_at=as_of + timedelta(days=14),
                    listing_status="LISTED",
                    source_id=self.identity,
                    dataset_version=self.adapter_version,
                )
            )
        return tuple(rows)

    def _on_feed_error(self, reason: str) -> dict[str, Any] | None:
        self._error = reason
        if self.settings.reconnect_policy == "bounded_backoff" and reason == "FEED_DISCONNECTED":
            self._state = SessionHealth.DEGRADED
            self._note("RECONNECTING", reason)
            return self._reconnect_poll()
        self._state = SessionHealth.DEGRADED
        raise GrowConfigError(reason)

    def _reconnect_poll(self) -> dict[str, Any] | None:
        if self._attempts >= self.settings.max_attempts:
            self._state = SessionHealth.STOPPED
            self._error = "FEED_DISCONNECTED"
            raise GrowConfigError("FEED_DISCONNECTED")
        delay = min(self.settings.max_backoff_seconds, max(1, 2 ** self._attempts))
        now = self.clock.now()
        if self._retry_at is None:
            self._retry_at = now + timedelta(seconds=delay)
        if now < self._retry_at:
            return None
        self._attempts += 1
        self.reconnect_count += 1
        try:
            self.transport.resubscribe(self._desired)
        except GrowConfigError as exc:
            self._retry_at = now + timedelta(seconds=min(self.settings.max_backoff_seconds, 2 ** self._attempts))
            self._error = str(exc)
            return None
        self._retry_at = None
        self._state = SessionHealth.RUNNING
        self._error = None
        self._note("RUNNING", "resubscribed")
        self.subscription_events.append(
            {
                "type": "RESUBSCRIBE",
                "symbols": list(self._desired),
                "reconnect_count": self.reconnect_count,
                "timestamp": now.isoformat(),
            }
        )
        return self.poll()

    def _note(self, state: str, reason: str) -> None:
        self.connection_events.append(
            {
                "timestamp": self.clock.now().isoformat(),
                "state": state,
                "reason": reason,
                "reconnect_count": self.reconnect_count,
                "provider": self.identity,
            }
        )


def settings_from_live_config(live: Any) -> TrueDataSettings:
    return TrueDataSettings(
        username_env=getattr(live, "truedata_username_env", "TRUEDATA_USERNAME"),
        password_env=getattr(live, "truedata_password_env", "TRUEDATA_PASSWORD"),
        endpoint=getattr(live, "truedata_endpoint", "push.truedata.in"),
        port=int(getattr(live, "truedata_port", 8082)),
        bid_ask=bool(getattr(live, "truedata_bid_ask", True)),
        max_symbols=int(getattr(live, "max_symbols", 50)),
        strike_window=int(getattr(live, "subscription_strike_window", 4)),
        reconnect_policy=str(getattr(live, "reconnect_policy", "bounded_backoff")),
        max_attempts=int(getattr(live, "reconnect_max_attempts", 5)),
        max_backoff_seconds=int(getattr(live, "reconnect_max_backoff_seconds", 30)),
        mode=str(getattr(live, "mode", "replay")),
    )
