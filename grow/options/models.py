"""2C option research contracts. Not orders. Not fills."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class ExpiryClass(str, Enum):
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class DecisionStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    NO_TRADE = "NO_TRADE"


class FieldSource(str, Enum):
    PROVIDER = "PROVIDER"
    COMPUTED = "COMPUTED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class OptionExpiry:
    day: date
    klass: ExpiryClass

    def to_dict(self) -> dict[str, str]:
        return {"date": self.day.isoformat(), "class": self.klass.value}


@dataclass(frozen=True)
class OptionContract:
    underlying: str
    expiry: date
    expiry_class: ExpiryClass
    strike: float
    option_type: OptionType
    bid: float | None
    ask: float | None
    last_price: float | None
    volume: int
    open_interest: int
    previous_open_interest: int | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    timestamp: datetime
    provider_contract_id: str
    iv_source: FieldSource = FieldSource.UNAVAILABLE
    greek_source: FieldSource = FieldSource.UNAVAILABLE

    def identity(self) -> tuple[str, str, float, str]:
        return (self.underlying, self.expiry.isoformat(), self.strike, self.option_type.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "expiry_class": self.expiry_class.value,
            "strike": self.strike,
            "option_type": self.option_type.value,
            "bid": self.bid,
            "ask": self.ask,
            "last_price": self.last_price,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "implied_volatility": self.implied_volatility,
            "delta": self.delta,
            "timestamp": self.timestamp.isoformat(),
            "provider_contract_id": self.provider_contract_id,
            "iv_source": self.iv_source.value,
            "greek_source": self.greek_source.value,
        }


@dataclass(frozen=True)
class OptionChainSnapshot:
    snapshot_id: str
    underlying: str
    as_of: datetime
    spot: float
    expiries: tuple[OptionExpiry, ...]
    contracts: tuple[OptionContract, ...]
    source_id: str
    is_fixture: bool
    provider_metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "underlying": self.underlying,
            "as_of": self.as_of.isoformat(),
            "spot": self.spot,
            "expiries": [e.to_dict() for e in self.expiries],
            "contract_count": len(self.contracts),
            "source_id": self.source_id,
            "is_fixture": self.is_fixture,
        }


@dataclass(frozen=True)
class ScoreBreakdown:
    version: str
    total: float
    components: Mapping[str, float]
    weights: Mapping[str, float]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "total": self.total,
            "components": dict(self.components),
            "weights": dict(self.weights),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class OptionCandidate:
    candidate_id: str
    underlying: str
    direction: str
    option_type: str
    intent: str
    expiry: date
    strike: float
    contract_symbol: str
    spot_price: float
    premium_reference: float
    bid: float | None
    ask: float | None
    spread: float | None
    spread_pct: float | None
    volume: int
    open_interest: int
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    intrinsic_value: float
    extrinsic_value: float
    moneyness: str
    score: ScoreBreakdown
    as_of: datetime
    underlying_snapshot_id: str
    option_chain_snapshot_id: str
    strategy_signal_id: str
    strategy_version: str
    selection_version: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        if self.intent != "BUY":
            raise ValueError("2C candidates are BUY only")
        if self.option_type not in {"CE", "PE"}:
            raise ValueError("2C option_type must be CE or PE")
        return {
            "candidate_id": self.candidate_id,
            "underlying": self.underlying,
            "direction": self.direction,
            "option_type": self.option_type,
            "intent": self.intent,
            "display": f"BUY {self.option_type}",
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "contract_symbol": self.contract_symbol,
            "spot_price": self.spot_price,
            "premium_reference": self.premium_reference,
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread,
            "spread_pct": self.spread_pct,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "implied_volatility": self.implied_volatility,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "intrinsic_value": self.intrinsic_value,
            "extrinsic_value": self.extrinsic_value,
            "moneyness": self.moneyness,
            "score": self.score.to_dict(),
            "as_of": self.as_of.isoformat(),
            "underlying_snapshot_id": self.underlying_snapshot_id,
            "option_chain_snapshot_id": self.option_chain_snapshot_id,
            "strategy_signal_id": self.strategy_signal_id,
            "strategy_version": self.strategy_version,
            "selection_version": self.selection_version,
            "reasons": list(self.reasons),
            "executed": False,
        }


@dataclass(frozen=True)
class RejectedContract:
    identity: tuple[str, str, float, str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.identity[0],
            "expiry": self.identity[1],
            "strike": self.identity[2],
            "option_type": self.identity[3],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OptionsDecision:
    status: DecisionStatus
    candidate: OptionCandidate | None
    rejected: tuple[RejectedContract, ...]
    diagnostics: tuple[str, ...]
    as_of: datetime
    strategy_signal_id: str
    option_chain_snapshot_id: str
    underlying_snapshot_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "candidate": None if self.candidate is None else self.candidate.to_dict(),
            "rejected": [r.to_dict() for r in self.rejected],
            "diagnostics": list(self.diagnostics),
            "as_of": self.as_of.isoformat(),
            "strategy_signal_id": self.strategy_signal_id,
            "option_chain_snapshot_id": self.option_chain_snapshot_id,
            "underlying_snapshot_id": self.underlying_snapshot_id,
            "executed": False,
        }


def candidate_id(
    *,
    underlying: str,
    expiry: str,
    strike: float,
    option_type: str,
    chain_id: str,
    signal_id: str,
    selection_version: str,
) -> str:
    body = json.dumps(
        {
            "chain_id": chain_id,
            "expiry": expiry,
            "option_type": option_type,
            "selection_version": selection_version,
            "signal_id": signal_id,
            "strike": strike,
            "underlying": underlying,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

