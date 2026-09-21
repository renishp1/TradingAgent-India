"""Append-only AI audit. Agents cannot delete or rewrite records."""

from __future__ import annotations

from grow.research.models import (
    DECISION_SCHEMA,
    PACKET_SCHEMA,
    REPORT_SCHEMA,
    AuditRecord,
    CEODecision,
    ResearchPacket,
    ResearchReport,
)


class AuditLog:
    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def append(
        self,
        packet: ResearchPacket,
        reports: tuple[ResearchReport, ...],
        decision: CEODecision,
        prompt_versions: dict[str, str],
    ) -> AuditRecord:
        record = AuditRecord(
            packet_id=packet.packet_id,
            report_ids=tuple(r.report_id for r in reports),
            decision_id=decision.decision_id,
            decision=decision.decision.value,
            candidate_id=decision.selected_candidate_id,
            snapshot_ids=packet.data_snapshot_ids,
            prompt_versions=prompt_versions,
            model_provider=decision.model_provider,
            validation_ok=decision.validation_ok,
            validation_failures=decision.validation_failures,
            as_of=packet.as_of,
            schema_versions={
                "packet": PACKET_SCHEMA,
                "report": REPORT_SCHEMA,
                "decision": DECISION_SCHEMA,
            },
        )
        self._records.append(record)
        return record

    def records(self) -> tuple[AuditRecord, ...]:
        return tuple(self._records)
