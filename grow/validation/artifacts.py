"""Versioned, reproducible evaluation artifacts.

Rerunning the same pinned code/data/configuration must reproduce results within
defined numerical tolerances.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from grow.errors import GrowConfigError

NUMERICAL_TOLERANCE = 1e-9
ARTIFACT_SCHEMA = "walkforward.artifact.v1"


@dataclass
class ArtifactStore:
    """In-memory artifact store with optional filesystem dump."""

    root: Path | None = None
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def put(self, key: str, payload: Mapping[str, Any]) -> str:
        body = {
            "schema": ARTIFACT_SCHEMA,
            "key": key,
            "payload": dict(payload),
            "live": False,
        }
        self.artifacts[key] = body
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f"{key}.json"
            path.write_text(json.dumps(body, sort_keys=True, indent=2, default=str), encoding="utf-8")
        return key

    def get(self, key: str) -> dict[str, Any]:
        if key not in self.artifacts:
            raise GrowConfigError(f"MISSING_ARTIFACT:{key}")
        return self.artifacts[key]

    def refs(self) -> dict[str, str]:
        return {key: f"artifact:{key}" for key in self.artifacts}


def metrics_equal(a: Mapping[str, Any], b: Mapping[str, Any], *, tol: float = NUMERICAL_TOLERANCE) -> bool:
    keys = set(a) | set(b)
    for key in keys:
        left, right = a.get(key), b.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if abs(float(left) - float(right)) > tol:
                return False
        elif left != right:
            return False
    return True


def assert_reproducible(first: Mapping[str, Any], second: Mapping[str, Any]) -> None:
    if not metrics_equal(first, second):
        raise GrowConfigError("REPLAY_NOT_DETERMINISTIC")
