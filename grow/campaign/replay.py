"""Phase 11 — durable decision / trade replay store (provider → fill → P&L).

Append-only audit records spanning campaign decisions through paper fills and
exits. Replay verifies deterministic outcomes given the same immutable inputs.
Paper-only; no broker path; not a third executor.
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from grow.campaign.runner import CampaignCycleResult
from grow.decision.integration.contract import IntegratedDecision
from grow.decision.integration.engine import DecisionEngine
from grow.errors import GrowSafetyError
from grow.market_data.normalized.models import AgentMarketSnapshot, OptionQuoteView
from grow.paper.checkpoint import atomic_write_json
from grow.paper.engine import PaperExecutionEngine, PaperExecutionResult
from grow.paper.positions import PaperPosition, PositionState


REPLAY_SCHEMA = "campaign.trade_replay.v1"

REQUIRED_RECORD_KEYS = (
    "snapshot_id",
    "cycle_id",
    "decision_id",
    "package_digest",
    "agent_versions",
    "market_data_provider",
    "contract_id",
    "expiry",
    "strike",
    "option_type",
    "bid",
    "ask",
    "ltp",
    "timestamp",
    "decision",
    "risk_guard_result",
    "paper_fill",
    "exit",
    "pnl",
)


@dataclass(frozen=True)
class TradeReplayRecord:
    """One durable trade-decision audit row spanning provider → fill → P&L."""

    snapshot_id: str
    cycle_id: str
    decision_id: str
    package_digest: str
    agent_versions: tuple[str, ...]
    market_data_provider: str
    contract_id: str | None
    expiry: str | None
    strike: float | None
    option_type: str | None
    bid: float | None
    ask: float | None
    ltp: float | None
    timestamp: str
    decision: Mapping[str, Any]
    risk_guard_result: str | None
    paper_fill: Mapping[str, Any] | None
    exit: Mapping[str, Any] | None
    pnl: Mapping[str, Any] | None
    book: Mapping[str, Any] | None = None
    package: Mapping[str, Any] | None = None
    snapshot: Mapping[str, Any] | None = None
    schema: str = REPLAY_SCHEMA
    paper_mode: bool = True
    live_trading: bool = False
    broker_order_path: bool = False
    broker_order_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "snapshot_id": self.snapshot_id,
            "cycle_id": self.cycle_id,
            "decision_id": self.decision_id,
            "package_digest": self.package_digest,
            "agent_versions": list(self.agent_versions),
            "market_data_provider": self.market_data_provider,
            "contract_id": self.contract_id,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "bid": self.bid,
            "ask": self.ask,
            "ltp": self.ltp,
            "timestamp": self.timestamp,
            "decision": dict(self.decision),
            "risk_guard_result": self.risk_guard_result,
            "paper_fill": None if self.paper_fill is None else dict(self.paper_fill),
            "exit": None if self.exit is None else dict(self.exit),
            "pnl": None if self.pnl is None else dict(self.pnl),
            "book": None if self.book is None else dict(self.book),
            "package": None if self.package is None else dict(self.package),
            "snapshot": None if self.snapshot is None else dict(self.snapshot),
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
            "broker_order_calls": 0,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TradeReplayRecord:
        validate_replay_payload(payload)
        return cls(
            snapshot_id=str(payload["snapshot_id"]),
            cycle_id=str(payload["cycle_id"]),
            decision_id=str(payload["decision_id"]),
            package_digest=str(payload["package_digest"]),
            agent_versions=tuple(str(row) for row in payload.get("agent_versions") or ()),
            market_data_provider=str(payload["market_data_provider"]),
            contract_id=None if payload.get("contract_id") is None else str(payload["contract_id"]),
            expiry=None if payload.get("expiry") is None else str(payload["expiry"]),
            strike=None if payload.get("strike") is None else float(payload["strike"]),
            option_type=None if payload.get("option_type") is None else str(payload["option_type"]),
            bid=None if payload.get("bid") is None else float(payload["bid"]),
            ask=None if payload.get("ask") is None else float(payload["ask"]),
            ltp=None if payload.get("ltp") is None else float(payload["ltp"]),
            timestamp=str(payload["timestamp"]),
            decision=dict(payload["decision"]),
            risk_guard_result=None
            if payload.get("risk_guard_result") is None
            else str(payload["risk_guard_result"]),
            paper_fill=None if payload.get("paper_fill") is None else dict(payload["paper_fill"]),
            exit=None if payload.get("exit") is None else dict(payload["exit"]),
            pnl=None if payload.get("pnl") is None else dict(payload["pnl"]),
            book=None if payload.get("book") is None else dict(payload["book"]),
            package=None if payload.get("package") is None else dict(payload["package"]),
            snapshot=None if payload.get("snapshot") is None else dict(payload["snapshot"]),
        )


def validate_replay_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise GrowSafetyError("trade replay payload is not a mapping")
    if payload.get("schema") != REPLAY_SCHEMA:
        raise GrowSafetyError("trade replay schema mismatch")
    if payload.get("live_trading") is True:
        raise GrowSafetyError("trade replay refused live_trading")
    if payload.get("paper_mode") is False:
        raise GrowSafetyError("trade replay requires paper_mode")
    if payload.get("broker_order_path") is True:
        raise GrowSafetyError("trade replay refused broker_order_path")
    for key in REQUIRED_RECORD_KEYS:
        if key not in payload:
            raise GrowSafetyError(f"trade replay missing {key}")
    return dict(payload)


def _quote_for_instrument(snapshot: AgentMarketSnapshot, instrument: str | None) -> OptionQuoteView | None:
    if not instrument:
        return None
    for quote in snapshot.option_contracts:
        if quote.provider_contract_id == instrument:
            return quote
    # Fallback: sole contract when instrument matches underlying-strike-type pattern.
    if len(snapshot.option_contracts) == 1:
        return snapshot.option_contracts[0]
    return None


def build_replay_record(
    cycle: CampaignCycleResult,
    snapshot: AgentMarketSnapshot,
    *,
    book: Mapping[str, Any] | None = None,
) -> TradeReplayRecord:
    """Build a durable replay row from one campaign cycle + source snapshot."""
    decision = cycle.decision
    execution = cycle.execution
    instrument = decision.candidate_instrument
    quote = _quote_for_instrument(snapshot, instrument)
    candidate = decision.trade_candidate
    agent_versions = tuple(
        f"{row.agent_name}@{row.agent_version}" for row in cycle.package.agent_outputs
    )
    fill: dict[str, Any] | None = None
    if execution.accepted:
        fill = {
            "accepted": True,
            "reason": execution.reason,
            "paper_order_id": execution.paper_order_id,
            "position_id": execution.position_id,
            "execution_price": execution.execution_price,
            "price_source": execution.price_source,
            "broker_order_calls": 0,
            "side": "BUY",
        }
    elif execution.reason:
        fill = {
            "accepted": False,
            "reason": execution.reason,
            "paper_order_id": execution.paper_order_id,
            "position_id": execution.position_id,
            "execution_price": execution.execution_price,
            "price_source": execution.price_source,
            "broker_order_calls": 0,
        }

    expiry = None
    strike = None
    option_type = None
    if candidate is not None:
        expiry = str(candidate.expiry)
        strike = float(candidate.strike)
        option_type = candidate.option_type
    elif quote is not None:
        expiry = quote.expiry.isoformat()
        strike = float(quote.strike)
        option_type = quote.option_type

    return TradeReplayRecord(
        snapshot_id=snapshot.snapshot_id,
        cycle_id=cycle.cycle_id,
        decision_id=decision.decision_id,
        package_digest=cycle.package.package_digest,
        agent_versions=agent_versions,
        market_data_provider=snapshot.provider,
        contract_id=None if quote is None else quote.provider_contract_id,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        bid=None if quote is None else quote.bid,
        ask=None if quote is None else quote.ask,
        ltp=None if quote is None else quote.ltp,
        timestamp=snapshot.decision_timestamp.isoformat(),
        decision=decision.to_dict(),
        risk_guard_result=decision.risk_guard_result,
        paper_fill=fill,
        exit=None,
        pnl=None,
        book=None if book is None else dict(book),
        package=cycle.package.to_dict(),
        snapshot=snapshot.to_dict(),
    )


def exit_payload_from_position(position: PaperPosition) -> dict[str, Any]:
    return {
        "position_id": position.position_id,
        "contract_id": position.contract_id,
        "exit_reason": position.exit_reason,
        "exit_price": position.current_price,
        "closed_at": None if position.closed_at is None else position.closed_at.isoformat(),
        "close_snapshot_id": position.close_snapshot_id,
        "side": "SELL",
    }


def pnl_payload_from_position(position: PaperPosition) -> dict[str, Any]:
    return {
        "realized_pnl": position.realized_pnl,
        "realized_gross": position.realized_gross,
        "total_costs": position.total_costs,
        "entry_price": position.entry_price,
        "exit_price": position.current_price,
        "quantity": position.quantity,
        "identity": "net == gross - costs",
        "net_equals_gross_minus_costs": abs(
            position.realized_pnl - (position.realized_gross - position.total_costs)
        )
        <= 1e-6,
    }


class TradeReplayStore:
    """Append-only durable store for campaign trade replay records.

    Durable append-only semantics are enforced under an OS-level exclusive
    ``fcntl.flock`` on a per-decision lock file so separate processes cannot
    race on the same ``decision_id``.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._by_id: dict[str, TradeReplayRecord] = {}
        self._load_existing()

    def _safe_id(self, decision_id: str) -> str:
        return decision_id.replace("/", "_")

    def _path_for(self, decision_id: str) -> Path:
        return self.root / f"{self._safe_id(decision_id)}.json"

    def _lock_path_for(self, decision_id: str) -> Path:
        return self.root / f".{self._safe_id(decision_id)}.lock"

    @contextmanager
    def _decision_lock(self, decision_id: str) -> Iterator[None]:
        """Exclusive cross-process critical section for one decision_id."""
        lock_path = self._lock_path_for(decision_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_disk(self, decision_id: str) -> TradeReplayRecord | None:
        path = self._path_for(decision_id)
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GrowSafetyError(f"trade replay unreadable: {path}") from exc
        if not raw.strip():
            raise GrowSafetyError(f"trade replay empty: {path}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GrowSafetyError(f"trade replay corrupt: {path}") from exc
        return TradeReplayRecord.from_dict(payload)

    def _load_existing(self) -> None:
        for path in sorted(self.root.glob("*.json")):
            if path.name.startswith("."):
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = TradeReplayRecord.from_dict(payload)
            self._by_id[record.decision_id] = record

    def __len__(self) -> int:
        return len(self._by_id)

    def decision_ids(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    def get(self, decision_id: str) -> TradeReplayRecord:
        # Prefer durable truth when present so a sibling process's write is visible.
        disk = self._read_disk(decision_id)
        if disk is not None:
            self._by_id[decision_id] = disk
            return disk
        try:
            return self._by_id[decision_id]
        except KeyError as exc:
            raise GrowSafetyError(f"UNKNOWN_REPLAY_DECISION:{decision_id}") from exc

    def append(self, record: TradeReplayRecord) -> TradeReplayRecord:
        """Durable append-only write under an exclusive filesystem lock."""
        with self._decision_lock(record.decision_id):
            disk = self._read_disk(record.decision_id)
            if disk is not None:
                if disk.to_dict() != record.to_dict():
                    raise GrowSafetyError(
                        f"trade replay is append-only; refusing overwrite of {record.decision_id}"
                    )
                self._by_id[record.decision_id] = disk
                return disk
            path = self._path_for(record.decision_id)
            atomic_write_json(path, record.to_dict())
            self._by_id[record.decision_id] = record
            return record

    def record_cycle(
        self,
        cycle: CampaignCycleResult,
        snapshot: AgentMarketSnapshot,
        *,
        book: Mapping[str, Any] | None = None,
    ) -> TradeReplayRecord:
        """Record the first durable row for a decision_id. Later cycles do not overwrite."""
        candidate = build_replay_record(cycle, snapshot, book=book)
        with self._decision_lock(candidate.decision_id):
            disk = self._read_disk(candidate.decision_id)
            if disk is not None:
                # First durable writer wins — later duplicate/reject cycles stay out.
                self._by_id[candidate.decision_id] = disk
                return disk
            atomic_write_json(self._path_for(candidate.decision_id), candidate.to_dict())
            self._by_id[candidate.decision_id] = candidate
            return candidate

    def attach_exit(
        self,
        decision_id: str,
        *,
        position: PaperPosition,
    ) -> TradeReplayRecord:
        """Attach exit + P&L once under an exclusive filesystem lock."""
        if position.state is not PositionState.CLOSED:
            raise GrowSafetyError("trade replay exit requires a CLOSED position")
        exit_row = exit_payload_from_position(position)
        pnl_row = pnl_payload_from_position(position)
        with self._decision_lock(decision_id):
            current = self._read_disk(decision_id)
            if current is None:
                raise GrowSafetyError(f"UNKNOWN_REPLAY_DECISION:{decision_id}")
            if current.exit is not None or current.pnl is not None:
                if current.exit == exit_row and current.pnl == pnl_row:
                    self._by_id[decision_id] = current
                    return current
                raise GrowSafetyError(
                    f"trade replay exit already recorded for {decision_id}; refusing overwrite"
                )
            fill = dict(current.paper_fill or {})
            if fill.get("position_id") and fill["position_id"] != position.position_id:
                raise GrowSafetyError("trade replay exit position_id mismatch")
            updated = TradeReplayRecord.from_dict(
                {
                    **current.to_dict(),
                    "exit": exit_row,
                    "pnl": pnl_row,
                }
            )
            atomic_write_json(self._path_for(decision_id), updated.to_dict())
            self._by_id[decision_id] = updated
            return updated


def replay_decision(engine: DecisionEngine, decision_id: str) -> IntegratedDecision:
    """Deterministic decision replay via DecisionEngine (Risk Guard preserved)."""
    return engine.replay(decision_id)


def verify_decision_replay(
    engine: DecisionEngine,
    store: TradeReplayStore,
    decision_id: str,
) -> IntegratedDecision:
    """Decision replay test helper: engine.replay matches durable store decision."""
    replayed = replay_decision(engine, decision_id)
    recorded = store.get(decision_id)
    if replayed.decision_id != recorded.decision_id:
        raise GrowSafetyError("decision replay decision_id mismatch")
    if replayed.action.value != recorded.decision.get("action"):
        raise GrowSafetyError("decision replay action mismatch")
    if replayed.status.value != recorded.decision.get("status"):
        raise GrowSafetyError("decision replay status mismatch")
    if replayed.risk_guard_result != recorded.risk_guard_result:
        raise GrowSafetyError("decision replay risk_guard_result mismatch")
    # Durable decision blob must match the live replayed decision core fields.
    live = replayed.to_dict()
    stored = recorded.decision
    for key in ("decision_id", "action", "status", "risk_guard_result", "snapshot_id", "analysis_cycle_id"):
        if live.get(key) != stored.get(key):
            raise GrowSafetyError(f"decision replay field mismatch: {key}")
    return replayed


def verify_paper_fill_replay(
    store: TradeReplayStore,
    decision_id: str,
    execution: PaperExecutionResult,
) -> Mapping[str, Any]:
    """Paper trade replay helper: durable fill matches the live execution result."""
    recorded = store.get(decision_id)
    fill = recorded.paper_fill
    if fill is None:
        raise GrowSafetyError("trade replay missing paper_fill")
    if bool(fill.get("accepted")) != bool(execution.accepted):
        raise GrowSafetyError("paper fill replay accepted mismatch")
    if fill.get("reason") != execution.reason:
        raise GrowSafetyError("paper fill replay reason mismatch")
    if execution.accepted:
        if fill.get("execution_price") != execution.execution_price:
            raise GrowSafetyError("paper fill replay price mismatch")
        if fill.get("price_source") != execution.price_source:
            raise GrowSafetyError("paper fill replay price_source mismatch")
        if fill.get("position_id") != execution.position_id:
            raise GrowSafetyError("paper fill replay position_id mismatch")
    if int(fill.get("broker_order_calls", 0)) != 0 or execution.broker_order_calls != 0:
        raise GrowSafetyError("paper fill replay broker_order_calls must remain 0")
    return fill


def verify_pnl_replay(store: TradeReplayStore, decision_id: str) -> Mapping[str, Any]:
    """P&L replay helper: stored identity holds and net matches gross − costs."""
    recorded = store.get(decision_id)
    pnl = recorded.pnl
    if pnl is None:
        raise GrowSafetyError("trade replay missing pnl")
    net = float(pnl["realized_pnl"])
    gross = float(pnl["realized_gross"])
    costs = float(pnl["total_costs"])
    if abs(net - (gross - costs)) > 1e-6:
        raise GrowSafetyError("pnl replay identity failed")
    if recorded.exit is None:
        raise GrowSafetyError("trade replay missing exit for pnl")
    return pnl


def sync_exits_from_paper(store: TradeReplayStore, paper: PaperExecutionEngine) -> tuple[str, ...]:
    """Attach exits/P&L for closed positions linked by paper_fill.position_id."""
    attached: list[str] = []
    by_position = {
        (row.paper_fill or {}).get("position_id"): decision_id
        for decision_id, row in ((did, store.get(did)) for did in store.decision_ids())
        if (row.paper_fill or {}).get("accepted") and (row.paper_fill or {}).get("position_id")
    }
    for position in paper.positions.all():
        if position.state is not PositionState.CLOSED:
            continue
        decision_id = by_position.get(position.position_id)
        if decision_id is None:
            continue
        store.attach_exit(decision_id, position=position)
        attached.append(decision_id)
    return tuple(attached)


__all__ = [
    "REPLAY_SCHEMA",
    "TradeReplayRecord",
    "TradeReplayStore",
    "build_replay_record",
    "replay_decision",
    "sync_exits_from_paper",
    "verify_decision_replay",
    "verify_paper_fill_replay",
    "verify_pnl_replay",
]
