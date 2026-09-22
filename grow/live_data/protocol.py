"""TrueData WebSocket protocol decoder. Isolated from trading decisions.

Vendor frames are classified before any tick ingest. Heartbeats and
subscription acks are never treated as market ticks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from grow.errors import GrowConfigError

AUTH = "auth"
HEARTBEAT = "heartbeat"
SUBSCRIBE = "subscribe"
UNSUBSCRIBE = "unsubscribe"
TICK = "tick"
ERROR = "error"
DISCONNECT = "disconnect"
CATALOG = "catalog"
SNAPSHOT = "snapshot"
CONTROL = "control"
MALFORMED = "malformed"

CONTROL_KINDS = frozenset({AUTH, HEARTBEAT, SUBSCRIBE, UNSUBSCRIBE, CONTROL})


@dataclass(frozen=True)
class ProtocolMessage:
    kind: str
    ok: bool | None
    payload: Mapping[str, Any]
    raw: str

    def to_dict(self) -> dict[str, Any]:
        body = dict(self.payload)
        body.setdefault("kind", self.kind)
        if self.ok is not None:
            body.setdefault("ok", self.ok)
        return body


def decode_truedata_message(raw: Any) -> ProtocolMessage:
    if isinstance(raw, ProtocolMessage):
        return raw
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GrowConfigError("MALFORMED_MESSAGE") from exc
    if isinstance(raw, Mapping):
        return _from_mapping(raw, json.dumps(raw, default=str))
    if not isinstance(raw, str):
        raise GrowConfigError("MALFORMED_MESSAGE")
    text = raw.strip()
    if not text:
        raise GrowConfigError("MALFORMED_MESSAGE")
    if text[0] in "{[":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GrowConfigError("MALFORMED_MESSAGE") from exc
        if isinstance(parsed, Mapping):
            return _from_mapping(parsed, text)
        raise GrowConfigError("MALFORMED_MESSAGE")
    lower = text.lower()
    if "heartbeat" in lower:
        return ProtocolMessage(HEARTBEAT, True, {"message": text}, text)
    if "disconnected" in lower:
        return ProtocolMessage(DISCONNECT, False, {"message": text}, text)
    if "fail" in lower or "unauthorized" in lower or "invalid login" in lower:
        return ProtocolMessage(AUTH, False, {"message": text, "ok": False}, text)
    if "truedata" in lower and "success" in lower:
        return ProtocolMessage(AUTH, True, {"message": text, "ok": True}, text)
    return ProtocolMessage(TICK, True, {"kind": "tick_csv", "line": text}, text)


def _from_mapping(data: Mapping[str, Any], raw: str) -> ProtocolMessage:
    if data.get("schema") == "grow.stream.snapshot.v1" or data.get("contract_master") is not None:
        return ProtocolMessage(SNAPSHOT, True, dict(data), raw)
    kind = str(data.get("kind") or "").lower()
    if kind == AUTH or kind == "login":
        ok = data.get("ok")
        if ok is None:
            ok = bool(data.get("success", True))
        return ProtocolMessage(AUTH, bool(ok), dict(data), raw)
    if kind == HEARTBEAT:
        return ProtocolMessage(HEARTBEAT, True, dict(data), raw)
    if kind in {SUBSCRIBE, "subscription", "symbolsadded"}:
        payload = dict(data)
        payload["mapping"] = extract_symbol_id_map(data)
        return ProtocolMessage(SUBSCRIBE, True, payload, raw)
    if kind == UNSUBSCRIBE:
        return ProtocolMessage(UNSUBSCRIBE, True, dict(data), raw)
    if kind in {TICK, "tick_csv", "trade", "touchline", "bidask"}:
        return ProtocolMessage(TICK, True, dict(data), raw)
    if kind == CATALOG:
        return ProtocolMessage(CATALOG, True, dict(data), raw)
    if kind == ERROR:
        return ProtocolMessage(ERROR, False, dict(data), raw)
    if kind == DISCONNECT:
        return ProtocolMessage(DISCONNECT, False, dict(data), raw)
    if kind == SNAPSHOT:
        return ProtocolMessage(SNAPSHOT, True, dict(data), raw)
    if _has_key(data, "heartbeat"):
        return ProtocolMessage(HEARTBEAT, True, dict(data), raw)
    if _has_key(data, "trade") or _has_key(data, "tick") or _has_key(data, "touchline") or _has_key(data, "bidask"):
        return ProtocolMessage(TICK, True, dict(data), raw)
    if _has_key(data, "symbolsadded") or _has_key(data, "symbollist") or "symbols added" in str(data.get("message") or "").lower():
        payload = dict(data)
        payload["mapping"] = extract_symbol_id_map(data)
        return ProtocolMessage(SUBSCRIBE, True, payload, raw)
    if _has_key(data, "instruments") or _has_key(data, "catalog"):
        return ProtocolMessage(CATALOG, True, dict(data), raw)
    message = str(data.get("error") or data.get("message") or "")
    lower = message.lower()
    if _has_key(data, "error") or data.get("success") is False:
        if "disconnect" in lower:
            return ProtocolMessage(DISCONNECT, False, dict(data), raw)
        if "auth" in lower or "login" in lower or "unauthorized" in lower:
            return ProtocolMessage(AUTH, False, dict(data), raw)
        return ProtocolMessage(ERROR, False, dict(data), raw)
    if "disconnected" in lower or _has_key(data, "disconnect"):
        return ProtocolMessage(DISCONNECT, False, dict(data), raw)
    if data.get("success") is True or "truedata" in str(data.get("message") or "").lower():
        return ProtocolMessage(AUTH, True, dict(data), raw)
    if data.get("symbol") or data.get("symbol_id") or data.get("symbolid"):
        return ProtocolMessage(TICK, True, dict(data), raw)
    raise GrowConfigError("MALFORMED_MESSAGE")


def extract_symbol_id_map(data: Mapping[str, Any]) -> dict[str, str]:
    """symbol_id -> provider_symbol. Never invents names."""
    mapping: dict[str, str] = {}
    if data.get("mapping") and isinstance(data["mapping"], Mapping):
        for key, value in data["mapping"].items():
            _put_map(mapping, key, value)
        return mapping
    rows = data.get("symbolsadded") or data.get("symbollist") or data.get("symbols") or ()
    if isinstance(rows, Mapping):
        for key, value in rows.items():
            _put_map(mapping, key, value)
        return mapping
    for row in rows:
        if isinstance(row, Mapping):
            symbol = row.get("symbol") or row.get("provider_symbol") or row.get("Symbol")
            ident = row.get("symbolid") or row.get("symbol_id") or row.get("id") or row.get("SymbolId")
            if symbol in (None, "") or ident in (None, ""):
                continue
            mapping[str(ident)] = str(symbol)
            continue
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            first, second = row[0], row[1]
            if _looks_id(first) and not _looks_id(second):
                mapping[str(first)] = str(second)
            elif _looks_id(second) and not _looks_id(first):
                mapping[str(second)] = str(first)
            elif isinstance(first, str) and not str(first).isdigit():
                mapping[str(second)] = str(first)
            else:
                mapping[str(first)] = str(second)
    return mapping


def _put_map(mapping: dict[str, str], key: Any, value: Any) -> None:
    if _looks_id(key) and not _looks_id(value):
        mapping[str(key)] = str(value)
    elif _looks_id(value) and not _looks_id(key):
        mapping[str(value)] = str(key)
    else:
        mapping[str(key)] = str(value)


def _looks_id(value: Any) -> bool:
    text = str(value).strip()
    return text.isdigit()


def _has_key(data: Mapping[str, Any], name: str) -> bool:
    lower = name.lower()
    return any(str(key).lower() == lower for key in data.keys())
