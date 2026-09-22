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
from grow.live_data.symbols import ParsedInstrument, parse_provider_symbol, parse_tick_fields
from grow.live_data.subscribe import plan_subscriptions, resubscribe_set
from grow.live_data.catalog import (
    DEFAULT_CATALOG_URLS,
    UNKNOWN_EXPIRY_CLASS,
    is_tradable_expiry_class,
    merge_catalog,
    normalize_catalog_row,
    parse_catalog_text,
)
from grow.live_data.protocol import (
    AUTH,
    CATALOG,
    CONTROL_KINDS,
    DISCONNECT,
    ERROR,
    HEARTBEAT,
    SNAPSHOT,
    SUBSCRIBE,
    TICK,
    ProtocolMessage,
    decode_truedata_message,
)

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

    def fetch_catalog(self) -> list[dict[str, Any]]:
        if self.catalog_rows:
            return merge_catalog(self.catalog_rows)
        rows: list[dict[str, Any]] = []
        for event in self.events:
            if event.get("kind") == "catalog":
                rows.extend(event.get("instruments") or [])
        return merge_catalog(rows) if rows else []

    def resubscribe(self, symbols: Sequence[str]) -> None:
        self.reconnects += 1
        self.connected = True
        self.subscribed = []
        self.subscribe(symbols)


class RealTrueDataTransport:
    """WebSocket transport + catalog bootstrap. Optional websocket-client. No broker."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        endpoint: str,
        port: int,
        bid_ask: bool = True,
        socket_factory: Any | None = None,
        catalog_loader: Any | None = None,
        catalog_urls: Sequence[str] | None = None,
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
        self._socket_factory = socket_factory
        self._catalog_loader = catalog_loader
        self._catalog_urls = tuple(catalog_urls or DEFAULT_CATALOG_URLS)
        self.sent: list[str] = []
        self.spot_bars: list[dict[str, Any]] = []

    def connect(self) -> None:
        try:
            if self._socket_factory is not None:
                self._ws = self._socket_factory()
            else:
                import importlib

                ws_mod = importlib.import_module("websocket")
                url = self.endpoint
                if "://" not in url:
                    url = f"wss://{url}:{self.port}"
                self._ws = ws_mod.create_connection(url, timeout=10)
            self._ws.send(f"{self.username}:{self.password}")
            self.sent.append("auth")
            reply = self._ws.recv()
        except GrowConfigError:
            raise
        except Exception as exc:
            raise GrowConfigError("AUTH_FAILED") from exc
        try:
            message = decode_truedata_message(reply)
        except GrowConfigError as exc:
            self.disconnect()
            raise GrowConfigError("AUTH_FAILED") from exc
        if message.kind != AUTH or message.ok is False:
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
        self.sent.append(payload)
        self.subscribed = list(dict.fromkeys([*self.subscribed, *symbols]))

    def unsubscribe(self, symbols: Sequence[str]) -> None:
        if self._ws is not None and self.connected:
            payload = "unsymbol:" + "+".join(symbols)
            self._ws.send(payload)
            self.sent.append(payload)
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
        message = decode_truedata_message(raw)
        if message.kind == DISCONNECT:
            self.connected = False
            raise GrowConfigError("FEED_DISCONNECTED")
        return message.to_dict()

    def fetch_catalog(self) -> list[dict[str, Any]]:
        if self._catalog_loader is not None:
            loaded = self._catalog_loader()
            if isinstance(loaded, str):
                return parse_catalog_text(loaded)
            if isinstance(loaded, Mapping) and loaded.get("instruments") is not None:
                self.spot_bars = list(loaded.get("spot_bars") or [])
                return merge_catalog(loaded.get("instruments") or [])
            return merge_catalog(loaded or [])
        rows: list[dict[str, Any]] = []
        try:
            import importlib

            request_mod = importlib.import_module("urllib.request")
            error_mod = importlib.import_module("urllib.error")
        except ImportError as exc:
            raise GrowConfigError("METADATA_UNAVAILABLE") from exc
        last_error: Exception | None = None
        for url in self._catalog_urls:
            try:
                with request_mod.urlopen(url, timeout=10) as response:
                    text = response.read().decode("utf-8")
                rows.extend(parse_catalog_text(text, source=url))
            except Exception as exc:  # noqa: BLE001 — fail closed after all URLs
                last_error = exc
                continue
        if not rows:
            raise GrowConfigError("METADATA_UNAVAILABLE") from last_error
        return merge_catalog(rows)

    def resubscribe(self, symbols: Sequence[str]) -> None:
        self.disconnect()
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
    max_staleness_seconds: int = 30


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
        socket_factory: Any | None = None,
        catalog_loader: Any | None = None,
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
        self._plan_spots: dict[str, float] = {}
        self._bars: list[dict[str, Any]] = []
        self._symbol_seq: dict[str, int] = {}
        self._symbol_ids: dict[str, str] = {}
        self._mapping_ready = False
        self._last_heartbeat_at: datetime | None = None
        self._last_activity_at: datetime | None = None
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
                socket_factory=socket_factory,
                catalog_loader=catalog_loader,
            )
        self._instruments: list[dict[str, Any]] = []
        for row in catalog or ():
            self._upsert_instrument(row)
        if not self._instruments and isinstance(self.transport, ReplayTransport):
            for row in self.transport.catalog_rows:
                self._upsert_instrument(row)
        seed_events: Sequence[Mapping[str, Any]] = events or ()
        if not seed_events and isinstance(self.transport, ReplayTransport):
            seed_events = self.transport.events
        self._seed_catalog_from_events(seed_events)

    def _upsert_instrument(self, row: Mapping[str, Any]) -> dict[str, Any]:
        item = normalize_catalog_row(row)
        existing = {rec["provider_symbol"]: rec for rec in self._instruments}
        existing[item["provider_symbol"]] = item
        self._instruments = list(existing.values())
        return item

    def _seed_catalog_from_events(self, events: Sequence[Mapping[str, Any]]) -> None:
        for event in events:
            if event.get("kind") == "catalog":
                for row in event.get("instruments") or ():
                    self._upsert_instrument(row)
            master = event.get("contract_master")
            if master:
                for row in master:
                    self._ingest_master_row(row)

    def _ingest_master_row(self, row: Mapping[str, Any]) -> None:
        provider_symbol = str(row.get("tradingsymbol") or row.get("provider_symbol") or row.get("provider_contract_id") or "")
        if not provider_symbol:
            return
        payload = dict(row)
        payload.setdefault("provider_symbol", provider_symbol)
        try:
            self._upsert_instrument(payload)
        except GrowConfigError:
            return

    def _clear_symbol_map(self) -> None:
        self._symbol_ids = {}
        self._mapping_ready = False

    def _subscription_mapped(self) -> bool:
        if not self._symbol_ids:
            return False
        have = set(self._symbol_ids.values())
        return all(symbol in have for symbol in self._desired)

    def _note_unknown_expiry_classes(self) -> None:
        unknown = [
            str(row["provider_symbol"])
            for row in self._instruments
            if row.get("option_type") in {"CE", "PE"} and not is_tradable_expiry_class(row.get("expiry_class"))
        ]
        if not unknown:
            return
        self.subscription_events.append(
            {
                "type": "UNKNOWN_EXPIRY_CLASS",
                "reason": "UNKNOWN_EXPIRY_CLASS",
                "symbols": unknown,
                "timestamp": self.clock.now().isoformat(),
            }
        )

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
        self._last_activity_at = self.clock.now()
        try:
            catalog = self.transport.fetch_catalog()
        except GrowConfigError as exc:
            if isinstance(self.transport, RealTrueDataTransport) or "METADATA" in str(exc):
                self._state = SessionHealth.DEGRADED
                self._error = "METADATA_UNAVAILABLE"
                self._note("DEGRADED", "METADATA_UNAVAILABLE")
                raise GrowConfigError("METADATA_UNAVAILABLE") from exc
            catalog = []
        if catalog:
            for row in catalog:
                self._upsert_instrument(row)
        bars = getattr(self.transport, "spot_bars", None)
        if bars:
            self._bars = list(bars)
            for row in self._bars:
                symbol = str(row.get("underlying") or "")
                close = row.get("close")
                if symbol and close not in (None, ""):
                    self._plan_spots[symbol] = float(close)
        if not self._instruments and isinstance(self.transport, RealTrueDataTransport):
            self._state = SessionHealth.DEGRADED
            self._error = "METADATA_UNAVAILABLE"
            self._note("DEGRADED", "METADATA_UNAVAILABLE")
            raise GrowConfigError("METADATA_UNAVAILABLE")
        self._clear_symbol_map()
        self._note_unknown_expiry_classes()
        self._state = SessionHealth.READY
        self._note("READY", "authenticated")
        if self._instruments:
            self._refresh_plan()

    def disconnect(self) -> None:
        self.transport.disconnect()
        self._clear_symbol_map()
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
            last_heartbeat_at=self._last_heartbeat_at,
        )

    def instrument_catalog(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(row) for row in self._instruments)

    def market_status(self) -> dict[str, Any]:
        return {
            "provider": self.identity,
            "state": self._state.value,
            "reconnect_count": self.reconnect_count,
            "subscribed": list(self._desired),
            "mapping_ready": self._mapping_ready,
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
        self._mapping_ready = self._subscription_mapped()
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
        self._mapping_ready = self._subscription_mapped()

    def poll(self) -> dict[str, Any] | None:
        if self._state in {SessionHealth.DISCONNECTED, SessionHealth.STOPPED, SessionHealth.CONNECTING}:
            return None
        if self._activity_stale():
            self._state = SessionHealth.DEGRADED
            self._error = "STALE_REQUIRED_QUOTE"
            raise GrowConfigError("STALE_REQUIRED_QUOTE")
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
        try:
            message = decode_truedata_message(raw)
        except GrowConfigError:
            if raw.get("schema") == "grow.stream.snapshot.v1" or raw.get("contract_master") is not None:
                message = ProtocolMessage(SNAPSHOT, True, dict(raw), "")
            else:
                raise
        self._last_activity_at = self.clock.now()
        if message.kind == HEARTBEAT:
            self._last_heartbeat_at = self.clock.now()
            if self._state is SessionHealth.READY:
                self._state = SessionHealth.RUNNING
            return {"kind": HEARTBEAT, "provider": self.identity, "ok": True}
        if message.kind == AUTH:
            if message.ok is False:
                self._state = SessionHealth.STOPPED
                self._error = "AUTH_FAILED"
                raise GrowConfigError("AUTH_FAILED")
            return {"kind": AUTH, "provider": self.identity, "ok": True}
        if message.kind == SUBSCRIBE:
            self._ingest_symbol_map(message.payload.get("mapping") or {})
            self._mapping_ready = self._subscription_mapped()
            return {"kind": SUBSCRIBE, "provider": self.identity, "mapping": dict(self._symbol_ids), "mapping_ready": self._mapping_ready}
        if message.kind == ERROR:
            self._state = SessionHealth.DEGRADED
            self._error = "PROVIDER_ERROR"
            raise GrowConfigError("PROVIDER_ERROR")
        if message.kind == DISCONNECT:
            return self._on_feed_error("FEED_DISCONNECTED")
        if message.kind == CATALOG:
            self._instruments.extend(dict(row) for row in (message.payload.get("instruments") or raw.get("instruments") or ()))
            if raw.get("spot_bars"):
                self._bars = list(raw.get("spot_bars") or [])
            self._refresh_plan()
            return {"kind": CATALOG, "provider": self.identity}
        if message.kind == TICK or raw.get("kind") in {"tick", "tick_csv"}:
            self._ingest_tick(message.to_dict() if message.kind == TICK else raw)
            return self._assemble_snapshot()
        if message.kind == SNAPSHOT or raw.get("schema") == "grow.stream.snapshot.v1" or raw.get("contract_master") is not None:
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

    def _activity_stale(self) -> bool:
        if self._last_activity_at is None:
            return False
        age = (self.clock.now() - self._last_activity_at).total_seconds()
        return age > self.settings.max_staleness_seconds

    def _ingest_symbol_map(self, mapping: Mapping[str, Any]) -> None:
        for ident, symbol in mapping.items():
            name = str(symbol).strip()
            if not name:
                continue
            self._symbol_ids[str(ident)] = name

    def _resolve_provider_symbol(self, fields: Mapping[str, Any]) -> str:
        symbol = str(fields.get("provider_symbol") or "").strip()
        symbol_id = fields.get("symbol_id")
        uses_id = symbol_id not in (None, "") or symbol.isdigit()
        if uses_id:
            if not self._mapping_ready:
                raise GrowConfigError("SYMBOL_MAP_NOT_READY")
            ident = str(symbol_id) if symbol_id not in (None, "") else symbol
            mapped = self._symbol_ids.get(ident)
            if not mapped:
                raise GrowConfigError("UNKNOWN_SYMBOL_ID")
            if symbol and not symbol.isdigit() and symbol != mapped:
                raise GrowConfigError("UNKNOWN_SYMBOL_ID")
            return mapped
        if not symbol:
            raise GrowConfigError("UNKNOWN_INSTRUMENT")
        return symbol

    def _ingest_tick(self, raw: Mapping[str, Any]) -> None:
        if raw.get("kind") == "tick_csv":
            fields = parse_tick_fields(str(raw.get("line") or ""))
        else:
            fields = parse_tick_fields(raw)
        symbol = self._resolve_provider_symbol(fields)
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
            had_live = parsed.canonical_symbol in self._spots
            self._spots[parsed.canonical_symbol] = float(fields["ltp"])
            if not had_live:
                self._refresh_plan()
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
                klass = row.get("expiry_class")
                if not is_tradable_expiry_class(klass):
                    continue
                master.append(
                    {
                        "tradingsymbol": row["provider_symbol"],
                        "underlying": row["canonical_symbol"],
                        "expiry": None if row["expiry"] is None else row["expiry"].isoformat(),
                        "strike": row["strike"],
                        "option_type": row["option_type"],
                        "instrument_type": "OPTIDX",
                        "lot_size": row.get("lot_size"),
                        "expiry_class": str(klass).upper(),
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
        spots = {**self._plan_spots, **self._spots}
        selected: dict[str, date] = {}
        parsed = []
        for row in self._instruments:
            expiry = row.get("expiry")
            if row.get("option_type") in {"CE", "PE"} and not is_tradable_expiry_class(row.get("expiry_class")):
                continue
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
        self._note_unknown_expiry_classes()
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
        add, drop = resubscribe_set(self._desired, planned)
        if drop:
            self.transport.unsubscribe(drop)
        if add:
            self.transport.subscribe(add)
        if planned != self._desired:
            self._desired = planned
            self._mapping_ready = self._subscription_mapped()
            self.subscription_events.append(
                {
                    "type": "PLAN",
                    "symbols": list(planned),
                    "policy_fingerprint": self.registry.fingerprint,
                    "timestamp": as_of.isoformat(),
                }
            )
        else:
            self._desired = planned
            self._mapping_ready = self._subscription_mapped()

    def _expiry_records(self, symbol: str) -> tuple[HistoricalExpiryRecord, ...]:
        grouped: dict[tuple[date, str], list[datetime]] = {}
        as_of = self.clock.now()
        for row in self._instruments:
            if row["canonical_symbol"] != symbol or row.get("expiry") is None:
                continue
            expiry = row["expiry"] if isinstance(row["expiry"], date) else date.fromisoformat(str(row["expiry"]))
            klass = str(row.get("expiry_class") or UNKNOWN_EXPIRY_CLASS).upper()
            if klass in {"", "NONE"}:
                klass = UNKNOWN_EXPIRY_CLASS
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
        if reason == "FEED_DISCONNECTED":
            self._clear_symbol_map()
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
        self._clear_symbol_map()
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
                "mapping_ready": False,
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
        max_staleness_seconds=int(getattr(live, "max_staleness_seconds", 30)),
    )
