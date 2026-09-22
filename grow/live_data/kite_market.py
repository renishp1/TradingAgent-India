"""Kite market-data adapter. Quotes only. No orders. No broker SDK.

Downstream code receives the same normalized snapshot shape as every other
live provider. Exchange timestamps are kept. Receipt time is never written
back onto a quote to make it look fresh.
"""

from __future__ import annotations

import csv
import io
import json
import struct
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from grow.clock import IST, Clock, FrozenClock
from grow.errors import GrowConfigError
from grow.history.universe import IndexUniverseRegistry, default_index_registry, is_forbidden_instrument
from grow.live_data.catalog import is_tradable_expiry_class
from grow.live_data.expiry_class import ExpiryClassifier, classification_counts
from grow.live_data.models import KITE_MARKET_PROVIDER_ID, LiveHealth, SessionHealth
from grow.live_data.provider import _FORBIDDEN_FALLBACK

KITE_MARKET_ADAPTER_VERSION = "live_data.kite.market.v1"
INSTRUMENTS_URL = "https://api.kite.trade/instruments/NFO"
QUOTE_URL = "https://api.kite.trade/quote"
SOCKET_URL = "wss://ws.kite.trade"
UNDERLYING_ORDER = ("NIFTY", "BANKNIFTY")
INDEX_QUERY = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK"}
INDEX_TOKEN = {"NIFTY": 256265, "BANKNIFTY": 260105}
INDICES_SEGMENT = 9
_PACKET_LENGTHS = frozenset({8, 28, 32, 44, 184})


@dataclass(frozen=True)
class NfoOption:
    instrument_token: int
    tradingsymbol: str
    underlying: str
    expiry: date
    strike: float
    option_type: str
    lot_size: int | None


@dataclass(frozen=True)
class MarketTick:
    instrument_token: int
    last_price: float | None
    bid: float | None
    ask: float | None
    volume: int | None
    open_interest: int | None
    exchange_timestamp: datetime | None
    tradable: bool
    mode: str


@dataclass(frozen=True)
class NormalizedOptionQuote:
    """Provider-neutral option quote. Absent fields were not supplied."""

    provider: str
    provider_symbol_id: str
    provider_symbol: str
    canonical_id: str
    underlying: str
    expiry: str
    strike: float
    option_type: str
    quote_time: datetime
    received_time: datetime
    ltp: float | None
    bid: float | None
    ask: float | None
    volume: int | None
    open_interest: int | None

    def to_stream_quote(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "underlying": self.underlying,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "ts": self.quote_time.isoformat(),
            "provider_symbol": self.provider_symbol,
        }
        if self.ltp is not None:
            row["ltp"] = self.ltp
        if self.bid is not None:
            row["bid"] = self.bid
        if self.ask is not None:
            row["ask"] = self.ask
        if self.volume is not None:
            row["volume"] = self.volume
        if self.open_interest is not None:
            row["oi"] = self.open_interest
        return row


def load_kite_market_secrets(environ: Mapping[str, str]) -> tuple[str, str]:
    api_key = str(environ.get("KITE_API_KEY") or "").strip()
    access_token = str(environ.get("KITE_ACCESS_TOKEN") or "").strip()
    if not api_key or not access_token:
        raise GrowConfigError("AUTH_MISSING")
    return api_key, access_token


def parse_nfo_instruments(text: str) -> tuple[NfoOption, ...]:
    """NIFTY and BANKNIFTY CE/PE rows from an NFO instrument dump."""
    body = str(text or "").lstrip("\ufeff").strip()
    if not body:
        raise GrowConfigError("METADATA_UNAVAILABLE")
    reader = csv.DictReader(io.StringIO(body))
    if not reader.fieldnames:
        raise GrowConfigError("METADATA_UNAVAILABLE")
    found: list[NfoOption] = []
    for raw in reader:
        row = {(key or "").strip().lower(): (value or "").strip() for key, value in raw.items()}
        underlying = row.get("name", "").upper()
        if underlying not in UNDERLYING_ORDER:
            continue
        option_type = row.get("instrument_type", "").upper()
        if option_type not in {"CE", "PE"}:
            continue
        segment = row.get("segment", "").upper()
        exchange = row.get("exchange", "").upper()
        if segment not in {"", "NFO-OPT"} and exchange not in {"", "NFO"}:
            continue
        if exchange not in {"", "NFO"}:
            continue
        symbol = row.get("tradingsymbol", "")
        if not symbol:
            continue
        try:
            token = int(float(row["instrument_token"]))
            expiry = date.fromisoformat(row["expiry"])
            strike = float(row["strike"])
        except (KeyError, TypeError, ValueError):
            continue
        if token <= 0 or strike <= 0:
            continue
        lot_size = None
        if row.get("lot_size"):
            try:
                lot_size = int(float(row["lot_size"]))
            except (TypeError, ValueError):
                lot_size = None
        found.append(
            NfoOption(
                instrument_token=token,
                tradingsymbol=symbol,
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                lot_size=lot_size,
            )
        )
    return tuple(found)


def select_option_contract(
    options: Sequence[NfoOption],
    *,
    spots: Mapping[str, float],
    as_of: date,
    classifier: ExpiryClassifier,
) -> NfoOption:
    """Nearest live expiry, then the strike closest to spot. CE before PE."""
    grouped: dict[str, list[NfoOption]] = {name: [] for name in UNDERLYING_ORDER}
    for row in options:
        if row.expiry < as_of:
            continue
        classified = classifier.classify(
            provider_symbol=row.tradingsymbol,
            canonical_symbol=row.underlying,
            expiry=row.expiry,
            option_type=row.option_type,
            as_of=as_of,
        )
        if not is_tradable_expiry_class(classified.expiry_class):
            continue
        grouped[row.underlying].append(row)
    for underlying in UNDERLYING_ORDER:
        spot = spots.get(underlying)
        rows = grouped[underlying]
        if spot is None or not rows:
            continue
        expiry = min(row.expiry for row in rows)
        pool = [row for row in rows if row.expiry == expiry]
        strike = min((row.strike for row in pool), key=lambda value: (abs(value - float(spot)), value))
        at_strike = [row for row in pool if row.strike == strike]
        calls = [row for row in at_strike if row.option_type == "CE"]
        puts = [row for row in at_strike if row.option_type == "PE"]
        if calls:
            return calls[0]
        if puts:
            return puts[0]
    raise GrowConfigError("NO_VALID_OPTION")


def _epoch_to_ist(seconds: int) -> datetime | None:
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(IST)


def _u32(packet: bytes, start: int) -> int:
    return struct.unpack_from(">I", packet, start)[0]


def _price(packet: bytes, start: int, divisor: float) -> float:
    return _u32(packet, start) / divisor


def split_binary_packets(blob: bytes) -> list[bytes]:
    if len(blob) < 2:
        return []
    count = struct.unpack_from(">H", blob, 0)[0]
    offset = 2
    packets: list[bytes] = []
    for _ in range(count):
        if offset + 2 > len(blob):
            raise GrowConfigError("MALFORMED_MESSAGE")
        length = struct.unpack_from(">H", blob, offset)[0]
        offset += 2
        if length not in _PACKET_LENGTHS or offset + length > len(blob):
            raise GrowConfigError("MALFORMED_MESSAGE")
        packets.append(blob[offset : offset + length])
        offset += length
    return packets


def decode_market_packet(packet: bytes) -> MarketTick:
    if len(packet) not in _PACKET_LENGTHS:
        raise GrowConfigError("MALFORMED_MESSAGE")
    token = _u32(packet, 0)
    segment = token & 0xFF
    divisor = 100.0
    tradable = segment != INDICES_SEGMENT
    if len(packet) == 8:
        return MarketTick(token, _price(packet, 4, divisor), None, None, None, None, None, tradable, "ltp")
    if len(packet) in {28, 32}:
        stamp = _epoch_to_ist(_u32(packet, 28)) if len(packet) == 32 else None
        return MarketTick(token, _price(packet, 4, divisor), None, None, None, None, stamp, tradable, "index")
    bid = ask = None
    volume = _u32(packet, 16)
    oi = None
    stamp = None
    if len(packet) == 184:
        stamp = _epoch_to_ist(_u32(packet, 60))
        oi = _u32(packet, 48)
        bid_px = _price(packet, 68, divisor)
        ask_px = _price(packet, 128, divisor)
        bid = bid_px if bid_px > 0 else None
        ask = ask_px if ask_px > 0 else None
    return MarketTick(
        token,
        _price(packet, 4, divisor),
        bid,
        ask,
        volume,
        oi,
        stamp,
        tradable,
        "full" if len(packet) == 184 else "quote",
    )


def decode_binary_frame(blob: bytes) -> tuple[MarketTick, ...]:
    return tuple(decode_market_packet(packet) for packet in split_binary_packets(blob))


class ScriptedKiteTransport:
    """In-memory frames for parser tests. Not a live socket and not a fixture feed."""

    def __init__(
        self,
        *,
        instruments_csv: str,
        spots: Mapping[str, float],
        frames: Sequence[bytes | str] = (),
    ) -> None:
        self.instruments_csv = instruments_csv
        self.spots = {str(key).upper(): float(value) for key, value in spots.items()}
        self.frames = list(frames)
        self.connected = False
        self.subscribed: list[int] = []
        self.mode: str | None = None
        self._index = 0

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def fetch_instruments(self) -> str:
        return self.instruments_csv

    def fetch_index_quote(self, underlying: str) -> tuple[float, int]:
        name = underlying.upper()
        if name not in self.spots:
            raise GrowConfigError("METADATA_UNAVAILABLE")
        return self.spots[name], INDEX_TOKEN[name]

    def subscribe(self, tokens: Sequence[int], *, mode: str) -> None:
        if not self.connected:
            raise GrowConfigError("SUBSCRIPTION_FAILED")
        if mode != "full":
            raise GrowConfigError("SUBSCRIPTION_FAILED")
        self.subscribed = [int(token) for token in tokens]
        self.mode = mode

    def recv(self) -> bytes | str | None:
        if not self.connected:
            raise GrowConfigError("FEED_DISCONNECTED")
        if self._index >= len(self.frames):
            return None
        frame = self.frames[self._index]
        self._index += 1
        return frame


class RealKiteTransport:
    """NFO instrument dump, index quote, and market-data socket. No order route."""

    def __init__(self, *, api_key: str, access_token: str) -> None:
        if not api_key or not access_token:
            raise GrowConfigError("AUTH_MISSING")
        self._api_key = api_key
        self._access_token = access_token
        self.connected = False
        self.subscribed: list[int] = []
        self.mode: str | None = None
        self._ws = None

    def connect(self) -> None:
        import importlib

        try:
            ws_mod = importlib.import_module("websocket")
            url = f"{SOCKET_URL}?api_key={self._api_key}&access_token={self._access_token}"
            self._ws = ws_mod.create_connection(url, timeout=15)
        except GrowConfigError:
            raise
        except Exception as exc:
            raise GrowConfigError("AUTH_FAILED") from exc
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

    def fetch_instruments(self) -> str:
        payload = self._get(INSTRUMENTS_URL)
        return payload.decode("utf-8")

    def fetch_index_quote(self, underlying: str) -> tuple[float, int]:
        name = underlying.upper()
        query = INDEX_QUERY.get(name)
        if query is None:
            raise GrowConfigError("NO_VALID_OPTION")
        import importlib

        parse = importlib.import_module("urllib.parse")
        url = f"{QUOTE_URL}?i={parse.quote(query)}"
        body = json.loads(self._get(url).decode("utf-8"))
        if body.get("status") != "success":
            raise GrowConfigError("METADATA_UNAVAILABLE")
        data = body.get("data") or {}
        if not isinstance(data, Mapping) or not data:
            raise GrowConfigError("METADATA_UNAVAILABLE")
        row = next(iter(data.values()))
        if not isinstance(row, Mapping) or row.get("last_price") in (None, ""):
            raise GrowConfigError("METADATA_UNAVAILABLE")
        token = row.get("instrument_token")
        try:
            ident = int(token) if token not in (None, "") else INDEX_TOKEN[name]
            price = float(row["last_price"])
        except (TypeError, ValueError) as exc:
            raise GrowConfigError("METADATA_UNAVAILABLE") from exc
        return price, ident

    def subscribe(self, tokens: Sequence[int], *, mode: str) -> None:
        if not self.connected or self._ws is None:
            raise GrowConfigError("SUBSCRIPTION_FAILED")
        if mode != "full":
            raise GrowConfigError("SUBSCRIPTION_FAILED")
        idents = [int(token) for token in tokens]
        self._ws.send(json.dumps({"a": "subscribe", "v": idents}))
        self._ws.send(json.dumps({"a": "mode", "v": [mode, idents]}))
        self.subscribed = idents
        self.mode = mode

    def recv(self) -> bytes | str | None:
        if not self.connected or self._ws is None:
            raise GrowConfigError("FEED_DISCONNECTED")
        try:
            raw = self._ws.recv()
        except Exception as exc:
            raise GrowConfigError("FEED_DISCONNECTED") from exc
        if raw is None or raw == b"" or raw == "":
            return None
        return raw

    def _get(self, url: str) -> bytes:
        import importlib

        request_mod = importlib.import_module("urllib.request")
        error_mod = importlib.import_module("urllib.error")
        req = request_mod.Request(
            url,
            headers={
                "Authorization": f"token {self._api_key}:{self._access_token}",
                "X-Kite-Version": "3",
            },
        )
        try:
            with request_mod.urlopen(req, timeout=20) as response:
                return response.read()
        except error_mod.HTTPError as exc:
            if exc.code in {401, 403}:
                raise GrowConfigError("AUTH_FAILED") from exc
            raise GrowConfigError("METADATA_UNAVAILABLE") from exc
        except GrowConfigError:
            raise
        except Exception as exc:
            raise GrowConfigError("METADATA_UNAVAILABLE") from exc


class KiteMarketProvider:
    """Market-data provider. Poll returns normalized snapshots, never raw packets."""

    identity = KITE_MARKET_PROVIDER_ID
    adapter_version = KITE_MARKET_ADAPTER_VERSION

    def __init__(
        self,
        *,
        api_key: str | None = None,
        access_token: str | None = None,
        transport: ScriptedKiteTransport | RealKiteTransport | None = None,
        clock: Clock | None = None,
        registry: IndexUniverseRegistry | None = None,
        classifier: ExpiryClassifier | None = None,
    ) -> None:
        self.clock = clock or FrozenClock(datetime.now(tz=IST))
        self.registry = registry or default_index_registry()
        self.classifier = classifier or ExpiryClassifier(clock=self.clock)
        self._api_key = api_key or ""
        self._access_token = access_token or ""
        self.transport = transport
        self._state = SessionHealth.DISCONNECTED
        self._error: str | None = None
        self._last_at: datetime | None = None
        self._last_seq: int | None = None
        self._adapter_seq = 0
        self.reconnect_count = 0
        self.connection_events: list[dict[str, Any]] = []
        self.subscription_events: list[dict[str, Any]] = []
        self._symbol_ids: dict[str, str] = {}
        self._mapping_ready = False
        self._desired: tuple[str, ...] = ()
        self._instruments: list[dict[str, Any]] = []
        self._options: tuple[NfoOption, ...] = ()
        self._chain: tuple[NfoOption, ...] = ()
        self.selected: NfoOption | None = None
        self._spots: dict[str, float] = {}
        self._index_tokens: dict[int, str] = {}
        self._quote: NormalizedOptionQuote | None = None
        self._last_heartbeat_at: datetime | None = None

    def connect(self) -> None:
        self._state = SessionHealth.CONNECTING
        self._note("CONNECTING", "connect")
        if self.transport is None:
            self.transport = RealKiteTransport(api_key=self._api_key, access_token=self._access_token)
        try:
            self.transport.connect()
            catalog = self.transport.fetch_instruments()
        except GrowConfigError as exc:
            self._error = str(exc)
            self._state = SessionHealth.STOPPED if "AUTH" in str(exc) else SessionHealth.DEGRADED
            self._note(self._state.value, str(exc))
            raise
        self._options = parse_nfo_instruments(catalog)
        if not self._options:
            self._fail_metadata("METADATA_UNAVAILABLE")
        spots: dict[str, float] = {}
        index_tokens: dict[int, str] = {}
        for underlying in UNDERLYING_ORDER:
            if not any(row.underlying == underlying for row in self._options):
                continue
            try:
                price, token = self.transport.fetch_index_quote(underlying)
            except GrowConfigError:
                continue
            spots[underlying] = price
            index_tokens[int(token)] = underlying
        if not spots:
            self._fail_metadata("METADATA_UNAVAILABLE")
        try:
            selected = select_option_contract(
                self._options,
                spots=spots,
                as_of=self.clock.now().date(),
                classifier=self.classifier,
            )
        except GrowConfigError as exc:
            self._fail_metadata(str(exc))
            raise
        self.selected = selected
        self._spots = {selected.underlying: spots[selected.underlying]}
        index_token = next(token for token, name in index_tokens.items() if name == selected.underlying)
        self._index_tokens = {index_token: selected.underlying}
        self._chain = tuple(
            row
            for row in self._options
            if row.underlying == selected.underlying and row.expiry == selected.expiry and _classified_ok(self.classifier, row)
        )
        self._instruments = [_catalog_row(self.classifier, row) for row in self._chain]
        self._symbol_ids = {str(selected.instrument_token): selected.tradingsymbol}
        self._desired = (selected.tradingsymbol,)
        try:
            self.transport.subscribe((index_token, selected.instrument_token), mode="full")
        except GrowConfigError as exc:
            self._error = "SUBSCRIPTION_FAILED"
            self._state = SessionHealth.DEGRADED
            self._note("DEGRADED", "SUBSCRIPTION_FAILED")
            raise GrowConfigError("SUBSCRIPTION_FAILED") from exc
        self._mapping_ready = True
        self.subscription_events.append(
            {
                "type": "SUBSCRIBE",
                "symbols": [selected.tradingsymbol],
                "instrument_token": selected.instrument_token,
                "count": 1,
                "timestamp": self.clock.now().isoformat(),
            }
        )
        self._error = None
        self._state = SessionHealth.READY
        self._note("READY", "authenticated")

    def disconnect(self) -> None:
        if self.transport is not None:
            self.transport.disconnect()
        self._mapping_ready = False
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

    def discover_underlyings(self, day: date | None = None) -> tuple[str, ...]:
        if self.selected is None:
            return ()
        symbol = self.selected.underlying
        if is_forbidden_instrument(symbol):
            return ()
        day = day or self.clock.now().date()
        if not self.registry.allows(symbol, day):
            return ()
        return (symbol,)

    def poll(self) -> dict[str, Any] | None:
        if self._state in {SessionHealth.DISCONNECTED, SessionHealth.STOPPED, SessionHealth.CONNECTING}:
            return None
        if self.transport is None:
            return None
        try:
            raw = self.transport.recv()
        except GrowConfigError as exc:
            self._error = str(exc)
            self._state = SessionHealth.DEGRADED
            raise
        if raw is None:
            return None
        if isinstance(raw, str) or _looks_text(raw):
            return self._on_text(raw)
        try:
            ticks = decode_binary_frame(raw if isinstance(raw, bytes) else bytes(raw))
        except GrowConfigError:
            return self._control("MALFORMED_MESSAGE")
        if not ticks:
            self._last_heartbeat_at = self.clock.now()
            return {"kind": "heartbeat", "provider": self.identity, "ok": True}
        saw_option = False
        saw_index = False
        index_time: datetime | None = None
        for tick in ticks:
            if tick.instrument_token in self._index_tokens:
                if tick.last_price is not None:
                    self._spots[self._index_tokens[tick.instrument_token]] = tick.last_price
                saw_index = True
                index_time = tick.exchange_timestamp or index_time
                continue
            if str(tick.instrument_token) not in self._symbol_ids:
                continue
            quote = self._option_quote(tick)
            if quote is None:
                continue
            self._quote = quote
            saw_option = True
        if saw_option and self._quote is not None:
            return self._assemble(self._quote.quote_time, include_quote=True)
        if saw_index:
            event_time = index_time if index_time is not None and index_time <= self.clock.now() else self.clock.now()
            return self._assemble(event_time, include_quote=False)
        return self._control("UNSUPPORTED_MESSAGE")

    def _option_quote(self, tick: MarketTick) -> NormalizedOptionQuote | None:
        if self.selected is None or tick.exchange_timestamp is None:
            return None
        if tick.instrument_token != self.selected.instrument_token:
            return None
        if not tick.tradable or tick.mode != "full":
            return None
        row = self.selected
        expiry = row.expiry.isoformat()
        canonical = f"{row.underlying}-{expiry}-{int(row.strike)}-{row.option_type}"
        if canonical == row.tradingsymbol:
            return None
        return NormalizedOptionQuote(
            provider=self.identity,
            provider_symbol_id=str(row.instrument_token),
            provider_symbol=row.tradingsymbol,
            canonical_id=canonical,
            underlying=row.underlying,
            expiry=expiry,
            strike=row.strike,
            option_type=row.option_type,
            quote_time=tick.exchange_timestamp,
            received_time=self.clock.now(),
            ltp=tick.last_price,
            bid=tick.bid,
            ask=tick.ask,
            volume=tick.volume,
            open_interest=tick.open_interest,
        )

    def _assemble(self, event_time: datetime, *, include_quote: bool) -> dict[str, Any]:
        underlyings = list(self.discover_underlyings())
        if not underlyings or not self._spots:
            return self._control("NO_OPTION_QUOTE")
        self._adapter_seq += 1
        master = []
        for row in self._chain:
            classified = self.classifier.classify(
                provider_symbol=row.tradingsymbol,
                canonical_symbol=row.underlying,
                expiry=row.expiry,
                option_type=row.option_type,
                as_of=self.clock.now(),
            )
            if not is_tradable_expiry_class(classified.expiry_class):
                continue
            master.append(
                {
                    "tradingsymbol": row.tradingsymbol,
                    "underlying": row.underlying,
                    "expiry": row.expiry.isoformat(),
                    "strike": row.strike,
                    "option_type": row.option_type,
                    "instrument_type": "OPTIDX",
                    "lot_size": row.lot_size,
                    "expiry_class": classified.expiry_class,
                }
            )
        quotes = [self._quote.to_stream_quote()] if include_quote and self._quote is not None else []
        if self._state is SessionHealth.READY:
            self._state = SessionHealth.RUNNING
        self._last_at = event_time
        self._last_seq = self._adapter_seq
        return {
            "provider": self.identity,
            "adapter_version": self.adapter_version,
            "schema": "grow.stream.snapshot.v1",
            "sequence": self._adapter_seq,
            "event_time": event_time.isoformat(),
            "received_time": self.clock.now().isoformat(),
            "session_date": self.clock.now().date().isoformat(),
            "source_timezone": "Asia/Kolkata",
            "instrument_type": "OPTIDX",
            "underlyings": underlyings,
            "spots": dict(self._spots),
            "spot_bars": [],
            "contract_master": master,
            "option_quotes": quotes,
            "is_fixture": False,
            "snapshot_id": f"zd-{self._adapter_seq}-{uuid4().hex[:8]}",
            "classification": classification_counts(self._instruments),
        }

    def _on_text(self, raw: bytes | str) -> dict[str, Any]:
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return self._control("MALFORMED_MESSAGE")
        if not isinstance(data, Mapping):
            return self._control("MALFORMED_MESSAGE")
        if data.get("is_fixture") is True or str(data.get("provider") or "") in _FORBIDDEN_FALLBACK:
            self._error = "FIXTURE_FALLBACK_FORBIDDEN"
            self._state = SessionHealth.DEGRADED
            raise GrowConfigError("FIXTURE_FALLBACK_FORBIDDEN")
        kind = str(data.get("type") or "").lower()
        if kind == "error":
            self._error = "AUTH_FAILED"
            self._state = SessionHealth.STOPPED
            raise GrowConfigError("AUTH_FAILED")
        if kind == "order":
            return self._control("ORDER_IGNORED")
        return self._control("CONTROL")

    def _control(self, reason: str) -> dict[str, Any]:
        return {"kind": "control", "provider": self.identity, "reason": reason, "ok": True}

    def _fail_metadata(self, reason: str) -> None:
        self._error = reason
        self._state = SessionHealth.DEGRADED
        self._note("DEGRADED", reason)
        raise GrowConfigError(reason)

    def _note(self, state: str, reason: str) -> None:
        self.connection_events.append(
            {"state": state, "reason": reason, "timestamp": self.clock.now().isoformat()}
        )


def _classified_ok(classifier: ExpiryClassifier, row: NfoOption) -> bool:
    classified = classifier.classify(
        provider_symbol=row.tradingsymbol,
        canonical_symbol=row.underlying,
        expiry=row.expiry,
        option_type=row.option_type,
    )
    return is_tradable_expiry_class(classified.expiry_class)


def _catalog_row(classifier: ExpiryClassifier, row: NfoOption) -> dict[str, Any]:
    classified = classifier.classify(
        provider_symbol=row.tradingsymbol,
        canonical_symbol=row.underlying,
        expiry=row.expiry,
        option_type=row.option_type,
    )
    return {
        "provider_symbol": row.tradingsymbol,
        "canonical_symbol": row.underlying,
        "expiry": row.expiry,
        "strike": row.strike,
        "option_type": row.option_type,
        "instrument_token": row.instrument_token,
        "lot_size": row.lot_size,
        "expiry_class": classified.expiry_class,
        "classification": classified.to_dict(),
    }


def _looks_text(raw: bytes | str) -> bool:
    if isinstance(raw, str):
        return True
    if not raw:
        return False
    return raw[:1] in {b"{", b"["}
