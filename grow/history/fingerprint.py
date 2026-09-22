"""Deterministic dataset fingerprints. Order-independent."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def fingerprint(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
