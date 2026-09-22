"""TrueData instrument catalog parser. Not a 2E dataset. Not fixture market data."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from grow.errors import GrowConfigError
from grow.live_data.symbols import parse_provider_symbol

INDEX_LIST = "NSE_SPOT_INDEX"
OPTION_LIST = "NSE_INDICES_OPTIONS"
DEFAULT_CATALOG_URLS = (
    "https://www.truedata.in/downloads/symbol_lists/WEB_SOCKET_API_NSE_SPOT_INDEX.txt",
    "https://www.truedata.in/downloads/symbol_lists/WEB_SOCKET_API_NSE_INDICES_OPTIONS.txt",
    "https://www.truedata.in/downloads/symbol_lists/1.WEB_SOCKET_API_NSE_SPOT_INDEX.txt",
    "https://www.truedata.in/downloads/symbol_lists/5.WEB_SOCKET_API_NSE_INDICES_OPTIONS.txt",
)


def parse_catalog_text(text: str, *, source: str = OPTION_LIST) -> list[dict[str, Any]]:
    if text is None:
        raise GrowConfigError("METADATA_UNAVAILABLE")
    body = str(text).strip()
    if not body:
        return []
    if body[0] in "{[":
        import json

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise GrowConfigError("METADATA_UNAVAILABLE") from exc
        if isinstance(parsed, Mapping) and parsed.get("instruments"):
            return [normalize_catalog_row(row) for row in parsed["instruments"]]
        if isinstance(parsed, list):
            return [normalize_catalog_row(row) for row in parsed]
        raise GrowConfigError("METADATA_UNAVAILABLE")
    rows: list[dict[str, Any]] = []
    header: list[str] | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "," in stripped:
            parts = [p.strip() for p in stripped.split(",")]
            if header is None and any(not p.replace(".", "", 1).isdigit() for p in parts[1:] or [""]) and any(
                token.lower() in {"symbol", "lot", "lot_size", "expiry"} for token in parts
            ):
                header = [p.lower() for p in parts]
                continue
            if header:
                data = {header[i]: parts[i] if i < len(parts) else "" for i in range(len(header))}
                rows.append(normalize_catalog_row(data))
                continue
            rows.append(normalize_catalog_row({"provider_symbol": parts[0], "lot_size": parts[1] if len(parts) > 1 else None}))
            continue
        rows.append(normalize_catalog_row({"provider_symbol": stripped, "list": source}))
    return rows


def normalize_catalog_row(row: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(row, str):
        row = {"provider_symbol": row}
    provider_symbol = str(
        row.get("provider_symbol") or row.get("tradingsymbol") or row.get("symbol") or row.get("Symbol") or ""
    ).strip()
    if not provider_symbol:
        raise GrowConfigError("UNKNOWN_INSTRUMENT")
    parsed = parse_provider_symbol(provider_symbol)
    expiry = parsed.expiry
    if row.get("expiry"):
        expiry = row["expiry"] if isinstance(row["expiry"], date) else date.fromisoformat(str(row["expiry"]))
    lot = row.get("lot_size") if "lot_size" in row else row.get("lot")
    try:
        lot_size = None if lot in (None, "") else int(float(lot))
    except (TypeError, ValueError) as exc:
        raise GrowConfigError("INVALID_LOT_SIZE") from exc
    ident = row.get("symbol_id") or row.get("symbolid") or row.get("provider_symbol_id")
    klass = str(row.get("expiry_class") or ("WEEKLY" if parsed.option_type else "")).upper() or None
    return {
        "provider_symbol": provider_symbol,
        "provider_symbol_id": None if ident in (None, "") else str(ident),
        "canonical_symbol": str(row.get("underlying") or row.get("canonical_symbol") or parsed.canonical_symbol).upper(),
        "instrument_type": str(row.get("instrument_type") or parsed.instrument_type),
        "expiry": expiry,
        "strike": parsed.strike if row.get("strike") in (None, "") else float(row["strike"]),
        "option_type": parsed.option_type if row.get("option_type") in (None, "") else str(row["option_type"]).upper(),
        "lot_size": lot_size,
        "expiry_class": klass,
    }


def merge_catalog(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = normalize_catalog_row(row)
        merged[item["provider_symbol"]] = item
    return list(merged.values())
