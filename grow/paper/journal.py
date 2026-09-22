"""Append-only paper-trade journal. Historical records are never rewritten."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from grow.errors import GrowSafetyError


@dataclass(frozen=True)
class PaperJournalRecord:
    record_id: str
    sequence: int
    kind: str
    decision_id: str | None
    cycle_id: str | None
    snapshot_id: str | None
    snapshot_version: str | None
    agent_outputs: tuple[Mapping[str, Any], ...]
    payload: Mapping[str, Any]
    timestamp: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "decision_id": self.decision_id,
            "cycle_id": self.cycle_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "agent_outputs": [dict(row) for row in self.agent_outputs],
            "payload": dict(self.payload),
            "timestamp": self.timestamp.isoformat(),
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PaperJournalRecord:
        return cls(
            record_id=str(payload["record_id"]),
            sequence=int(payload["sequence"]),
            kind=str(payload["kind"]),
            decision_id=None if payload.get("decision_id") is None else str(payload["decision_id"]),
            cycle_id=None if payload.get("cycle_id") is None else str(payload["cycle_id"]),
            snapshot_id=None if payload.get("snapshot_id") is None else str(payload["snapshot_id"]),
            snapshot_version=None if payload.get("snapshot_version") is None else str(payload["snapshot_version"]),
            agent_outputs=tuple(dict(row) for row in payload.get("agent_outputs") or ()),
            payload=dict(payload.get("payload") or {}),
            timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        )


class PaperJournal:
    """Append-only. ``rewrite`` and ``delete`` always fail closed."""

    def __init__(self) -> None:
        self._records: list[PaperJournalRecord] = []

    def __len__(self) -> int:
        return len(self._records)

    @property
    def records(self) -> tuple[PaperJournalRecord, ...]:
        return tuple(self._records)

    def append(
        self,
        *,
        kind: str,
        timestamp: datetime,
        decision_id: str | None,
        cycle_id: str | None,
        snapshot_id: str | None,
        snapshot_version: str | None,
        agent_outputs: tuple[Mapping[str, Any], ...] = (),
        payload: Mapping[str, Any] | None = None,
        sequence: int | None = None,
        record_id: str | None = None,
    ) -> PaperJournalRecord:
        seq = len(self._records) + 1 if sequence is None else sequence
        if any(row.sequence == seq or row.record_id == (record_id or f"pj-{seq:06d}") for row in self._records):
            raise GrowSafetyError("paper journal refused a duplicate record")
        if sequence is not None and sequence != len(self._records) + 1:
            raise GrowSafetyError("paper journal refused an out-of-order record")
        record = PaperJournalRecord(
            record_id=record_id or f"pj-{seq:06d}",
            sequence=seq,
            kind=kind,
            decision_id=decision_id,
            cycle_id=cycle_id,
            snapshot_id=snapshot_id,
            snapshot_version=snapshot_version,
            agent_outputs=tuple(dict(row) for row in agent_outputs),
            payload=dict(payload or {}),
            timestamp=timestamp,
        )
        self._records.append(record)
        return record

    def load(self, rows: list[Mapping[str, Any]]) -> None:
        """Restore an exported journal. Refuses to replace a journal that already has rows."""
        if self._records:
            raise GrowSafetyError("paper journal is append-only; refusing to replace history")
        for row in rows:
            record = PaperJournalRecord.from_dict(row)
            if record.sequence != len(self._records) + 1:
                raise GrowSafetyError("paper journal history is not a contiguous append log")
            if any(existing.record_id == record.record_id for existing in self._records):
                raise GrowSafetyError("paper journal refused a duplicate historical record")
            self._records.append(record)

    def to_list(self) -> list[dict[str, Any]]:
        return [row.to_dict() for row in self._records]

    def rewrite(self, *_args: Any, **_kwargs: Any) -> None:
        raise GrowSafetyError("paper journal is append-only; historical records are not rewritten")

    def delete(self, *_args: Any, **_kwargs: Any) -> None:
        raise GrowSafetyError("paper journal is append-only; historical records are not deleted")
