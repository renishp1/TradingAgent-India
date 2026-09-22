"""Paper order contract. A record of a simulated order, not a broker order."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping


class PaperLifecycle(str, Enum):
    """Created → Pending → Filled → Open → Exit Requested → Closed / Rejected / Expired."""

    CREATED = "CREATED"
    PENDING = "PENDING"
    FILLED = "FILLED"
    OPEN = "OPEN"
    EXIT_REQUESTED = "EXIT_REQUESTED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class FillStatus(str, Enum):
    UNFILLED = "UNFILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class PaperOrder:
    paper_order_id: str
    decision_id: str
    analysis_cycle_id: str
    snapshot_id: str
    snapshot_version: str
    instrument: str
    token: str | None
    symbol: str
    expiry: date | None
    strike: float | None
    option_type: str | None
    quantity: int
    side: str
    direction: str | None
    order_type: str
    requested_price: float | None
    execution_price: float | None
    status: str
    fill_status: str
    rejection_reason: str | None
    fee_model_version: str
    slippage_model_version: str
    slippage_bps: float
    slippage: float
    price_source: str | None
    fee_assumptions: Mapping[str, Any]
    timestamp: datetime
    source_snapshot_id: str
    position_id: str | None = None
    journal_record_id: str | None = None
    exit_reason: str | None = None
    lot_size: int | None = None
    lots: int | None = None
    underlying: str | None = None
    reference_price: float | None = None
    paper_mode: bool = True
    live_trading: bool = False
    broker_order_path: bool = False

    def __post_init__(self) -> None:
        if self.live_trading or self.broker_order_path or not self.paper_mode:
            raise ValueError("paper orders stay paper-only")
        if self.status not in {item.value for item in PaperLifecycle}:
            raise ValueError(f"unsupported paper lifecycle {self.status}")
        if self.fill_status not in {item.value for item in FillStatus}:
            raise ValueError(f"unsupported fill status {self.fill_status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_order_id": self.paper_order_id,
            "decision_id": self.decision_id,
            "analysis_cycle_id": self.analysis_cycle_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "instrument": self.instrument,
            "token": self.token,
            "symbol": self.symbol,
            "underlying": self.underlying,
            "expiry": None if self.expiry is None else self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type,
            "lot_size": self.lot_size,
            "lots": self.lots,
            "quantity": self.quantity,
            "side": self.side,
            "direction": self.direction,
            "order_type": self.order_type,
            "requested_price": self.requested_price,
            "execution_price": self.execution_price,
            "reference_price": self.reference_price,
            "status": self.status,
            "fill_status": self.fill_status,
            "rejection_reason": self.rejection_reason,
            "fee_model_version": self.fee_model_version,
            "slippage_model_version": self.slippage_model_version,
            "fill_model_version": self.slippage_model_version,
            "slippage_bps": self.slippage_bps,
            "slippage": self.slippage,
            "price_source": self.price_source,
            "fee_assumptions": dict(self.fee_assumptions),
            "timestamp": self.timestamp.isoformat(),
            "source_snapshot_id": self.source_snapshot_id,
            "position_id": self.position_id,
            "journal_record_id": self.journal_record_id,
            "exit_reason": self.exit_reason,
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PaperOrder:
        expiry = payload.get("expiry")
        return cls(
            paper_order_id=str(payload["paper_order_id"]),
            decision_id=str(payload["decision_id"]),
            analysis_cycle_id=str(payload["analysis_cycle_id"]),
            snapshot_id=str(payload["snapshot_id"]),
            snapshot_version=str(payload["snapshot_version"]),
            instrument=str(payload["instrument"]),
            token=None if payload.get("token") is None else str(payload["token"]),
            symbol=str(payload["symbol"]),
            expiry=None if expiry is None else date.fromisoformat(str(expiry)),
            strike=None if payload.get("strike") is None else float(payload["strike"]),
            option_type=None if payload.get("option_type") is None else str(payload["option_type"]),
            quantity=int(payload["quantity"]),
            side=str(payload["side"]),
            direction=None if payload.get("direction") is None else str(payload["direction"]),
            order_type=str(payload["order_type"]),
            requested_price=None if payload.get("requested_price") is None else float(payload["requested_price"]),
            execution_price=None if payload.get("execution_price") is None else float(payload["execution_price"]),
            status=str(payload["status"]),
            fill_status=str(payload["fill_status"]),
            rejection_reason=None if payload.get("rejection_reason") is None else str(payload["rejection_reason"]),
            fee_model_version=str(payload["fee_model_version"]),
            slippage_model_version=str(payload["slippage_model_version"]),
            slippage_bps=float(payload["slippage_bps"]),
            slippage=float(payload["slippage"]),
            price_source=None if payload.get("price_source") is None else str(payload["price_source"]),
            fee_assumptions=dict(payload.get("fee_assumptions") or {}),
            timestamp=datetime.fromisoformat(str(payload["timestamp"])),
            source_snapshot_id=str(payload["source_snapshot_id"]),
            position_id=None if payload.get("position_id") is None else str(payload["position_id"]),
            journal_record_id=None if payload.get("journal_record_id") is None else str(payload["journal_record_id"]),
            exit_reason=None if payload.get("exit_reason") is None else str(payload["exit_reason"]),
            lot_size=None if payload.get("lot_size") is None else int(payload["lot_size"]),
            lots=None if payload.get("lots") is None else int(payload["lots"]),
            underlying=None if payload.get("underlying") is None else str(payload["underlying"]),
            reference_price=None if payload.get("reference_price") is None else float(payload["reference_price"]),
            paper_mode=True,
            live_trading=False,
            broker_order_path=False,
        )
