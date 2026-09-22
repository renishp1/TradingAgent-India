"""Phase 9 — durable paper checkpoint for cross-process restart/recovery.

Atomic filesystem checkpoints wrap ``PaperExecutionEngine.export_state``.
Corrupt, truncated, or live-trading payloads fail closed. This is not a
broker path and not a third executor.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from grow.clock import Clock
from grow.config import GrowConfig
from grow.errors import GrowSafetyError
from grow.paper.engine import PaperExecutionEngine


CHECKPOINT_SCHEMA = "paper.checkpoint.v1"
REQUIRED_ENGINE_KEYS = (
    "session_id",
    "started_at",
    "journal",
    "orders",
    "positions",
    "registry",
    "ledger",
)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically (temp file in the same directory + ``os.replace``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(dict(payload), sort_keys=True, indent=2, default=str) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def wrap_checkpoint(engine_state: Mapping[str, Any]) -> dict[str, Any]:
    """Envelope around an exported paper engine state."""
    if engine_state.get("live_trading") is True:
        raise GrowSafetyError("paper checkpoint refused live_trading state")
    if engine_state.get("paper_mode") is False:
        raise GrowSafetyError("paper checkpoint requires paper_mode")
    return {
        "schema": CHECKPOINT_SCHEMA,
        "paper_mode": True,
        "live_trading": False,
        "broker_order_path": False,
        "engine": dict(engine_state),
    }


def validate_checkpoint(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed on corrupt, truncated, or unsafe checkpoint payloads."""
    if not isinstance(payload, Mapping):
        raise GrowSafetyError("paper checkpoint payload is not a mapping")
    if payload.get("schema") != CHECKPOINT_SCHEMA:
        raise GrowSafetyError("paper checkpoint schema mismatch")
    if payload.get("live_trading") is True:
        raise GrowSafetyError("paper checkpoint refused live_trading envelope")
    if payload.get("paper_mode") is False:
        raise GrowSafetyError("paper checkpoint requires paper_mode envelope")
    if payload.get("broker_order_path") is True:
        raise GrowSafetyError("paper checkpoint refused broker_order_path")
    engine = payload.get("engine")
    if not isinstance(engine, Mapping):
        raise GrowSafetyError("paper checkpoint missing engine state")
    for key in REQUIRED_ENGINE_KEYS:
        if key not in engine:
            raise GrowSafetyError(f"paper checkpoint missing engine.{key}")
    if engine.get("live_trading") is True:
        raise GrowSafetyError("paper checkpoint refused live engine state")
    if engine.get("paper_mode") is False:
        raise GrowSafetyError("paper checkpoint requires paper engine state")
    journal = engine.get("journal")
    if not isinstance(journal, list):
        raise GrowSafetyError("paper checkpoint journal must be a list")
    return dict(payload)


def save_paper_checkpoint(path: Path | str, engine: PaperExecutionEngine) -> Path:
    """Persist a paper engine checkpoint to disk atomically."""
    target = Path(path)
    envelope = wrap_checkpoint(engine.export_state())
    atomic_write_json(target, envelope)
    return target


def load_paper_checkpoint(path: Path | str) -> dict[str, Any]:
    """Load and validate a durable paper checkpoint. Fail closed on corruption."""
    target = Path(path)
    if not target.is_file():
        raise GrowSafetyError(f"paper checkpoint missing: {target}")
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise GrowSafetyError(f"paper checkpoint unreadable: {target}") from exc
    if not raw.strip():
        raise GrowSafetyError("paper checkpoint is empty")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GrowSafetyError("paper checkpoint is corrupt or truncated") from exc
    return validate_checkpoint(payload)


def restore_paper_engine(
    config: GrowConfig,
    path: Path | str,
    *,
    clock: Clock | None = None,
    risk_secret: str | None = None,
) -> PaperExecutionEngine:
    """Cross-process restore: disk checkpoint → ``PaperExecutionEngine.restore``."""
    target = Path(path)
    payload = load_paper_checkpoint(target)
    return PaperExecutionEngine.restore(
        config,
        payload["engine"],
        clock=clock,
        risk_secret=risk_secret,
        checkpoint_path=target,
    )


class PaperCheckpointStore:
    """Filesystem-backed paper checkpoint for unattended restart recovery."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def save(self, engine: PaperExecutionEngine) -> Path:
        return save_paper_checkpoint(self.path, engine)

    def load(self) -> dict[str, Any]:
        return load_paper_checkpoint(self.path)

    def restore(
        self,
        config: GrowConfig,
        *,
        clock: Clock | None = None,
        risk_secret: str | None = None,
    ) -> PaperExecutionEngine:
        return restore_paper_engine(config, self.path, clock=clock, risk_secret=risk_secret)
