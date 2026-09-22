"""Historical replay of snapshot → 4C → paper execution contracts.

Uses the same paper-execution path as live paper trading. No broker orders.
Point-in-time quotes only. Future prices are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Mapping, Sequence

from grow.backtest.costs import CostModel
from grow.clock import IST, FrozenClock
from grow.config import GrowConfig
from grow.decision.integration.integrator import DecisionIntegrator
from grow.errors import GrowConfigError
from grow.market_data.normalized.models import AgentMarketSnapshot
from grow.orchestration.models import AggregateAnalysisPackage
from grow.paper.engine import PaperExecutionEngine
from grow.paper.fills import policy_from_config
from grow.validation.availability import quote_on_historical_chain
from grow.validation.metrics import calculate_metrics
from grow.validation.pit import snapshot_leakage_flags
from grow.validation.windows import sessions_in_range


CycleBuilder = Callable[[AgentMarketSnapshot], AggregateAnalysisPackage]


@dataclass(frozen=True)
class HistoricalCycle:
    """One point-in-time market moment with an optional prebuilt package."""

    snapshot: AgentMarketSnapshot
    package: AggregateAnalysisPackage | None = None
    session_date: date | None = None
    regime: str = "UNKNOWN"

    @property
    def day(self) -> date:
        if self.session_date is not None:
            return self.session_date
        return self.snapshot.decision_timestamp.astimezone(IST).date()


@dataclass
class ReplayResult:
    role: str
    metrics: dict[str, Any]
    trades: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    leakage_flags: list[str] = field(default_factory=list)
    cycle_count: int = 0
    candidate_count: int = 0
    journal: list[dict[str, Any]] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "metrics": self.metrics,
            "trades": list(self.trades),
            "decisions": list(self.decisions),
            "rejections": list(self.rejections),
            "leakage_flags": list(self.leakage_flags),
            "cycle_count": self.cycle_count,
            "candidate_count": self.candidate_count,
            "journal": list(self.journal),
            "equity": list(self.equity),
            "live": False,
        }


class HistoricalPaperReplay:
    """Drive frozen paper execution across chronological historical cycles."""

    def __init__(
        self,
        config: GrowConfig,
        *,
        risk_secret: str,
        cost_stress: float = 1.0,
        listed_contracts: Mapping[str, Any] | None = None,
    ) -> None:
        config.assert_safe()
        if config.execution.live_trading_enabled or config.live_data.live_trading or not config.live_data.paper_mode:
            raise GrowConfigError("PAPER_ONLY_REPLAY")
        self.config = config
        self.risk_secret = risk_secret
        self.cost_stress = cost_stress
        self.listed_contracts = listed_contracts
        self.policy = policy_from_config(config)
        self.costs = CostModel(stress=cost_stress)

    def run(
        self,
        cycles: Sequence[HistoricalCycle],
        *,
        role: str,
        start: date,
        end: date,
        package_builder: CycleBuilder | None = None,
    ) -> ReplayResult:
        selected = [c for c in cycles if start <= c.day <= end]
        selected = sorted(selected, key=lambda c: c.snapshot.decision_timestamp.astimezone(IST))
        if not selected:
            metrics = calculate_metrics(
                trades=[],
                decisions=[],
                equity=[self.config.paper.starting_cash],
                starting_cash=self.config.paper.starting_cash,
            )
            return ReplayResult(role=role, metrics=metrics, equity=[self.config.paper.starting_cash])

        clock = FrozenClock(selected[0].snapshot.decision_timestamp.astimezone(IST))
        engine = PaperExecutionEngine(self.config, clock=clock, risk_secret=self.risk_secret)
        integrator = DecisionIntegrator(self.config, risk_guard=engine.guard)
        trades: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        leakage: list[str] = []
        candidates = 0
        daily_loss_breaches = 0
        per_trade_risk_breaches = 0
        blocked = 0
        data_quality_failures = 0
        by_regime: dict[str, int] = {}
        by_expiry: dict[str, int] = {}
        by_instrument: dict[str, int] = {}
        by_bucket: dict[str, int] = {}
        durations: list[float] = []
        exposure = 0.0
        equity = [self.config.paper.starting_cash]
        open_meta: dict[str, dict[str, Any]] = {}
        recorded_closed: set[str] = set()

        for cycle in selected:
            snap = cycle.snapshot
            moment = snap.decision_timestamp.astimezone(IST)
            clock._when = moment
            engine.clock = clock
            engine.guard.clock = clock
            engine.ledger.clock = clock

            flags = list(snapshot_leakage_flags(snap))
            leakage.extend(flags)
            skip_entry = bool(flags)
            if flags:
                decisions.append(
                    {
                        "status": "NO_TRADE",
                        "reason": flags[0],
                        "as_of": moment.isoformat(),
                        "underlying": next(iter(snap.underlyings), None),
                    }
                )
                data_quality_failures += 1

            unavailable = False
            if not skip_entry and self.listed_contracts is not None:
                for quote in snap.option_contracts:
                    avail = quote_on_historical_chain(
                        quote,
                        decision_at=moment,
                        listed=self.listed_contracts,
                    )
                    if not avail.available:
                        leakage.append(avail.reason)
                        unavailable = True
                        decisions.append(
                            {
                                "status": "NO_TRADE",
                                "reason": avail.reason,
                                "as_of": moment.isoformat(),
                                "instrument": quote.provider_contract_id,
                            }
                        )
                        break

            if not skip_entry and not unavailable:
                package = cycle.package
                if package is None:
                    if package_builder is None:
                        raise GrowConfigError("MISSING_CYCLE_PACKAGE")
                    package = package_builder(snap)
                result = engine.run(integrator, snap, package)
                decisions.append(
                    {
                        "status": "FILLED" if result.accepted else "NO_TRADE",
                        "reason": result.reason,
                        "as_of": moment.isoformat(),
                        "decision_id": result.decision_id,
                        "paper_order_id": result.paper_order_id,
                    }
                )
                if result.accepted:
                    candidates += 1
                    by_regime[cycle.regime] = by_regime.get(cycle.regime, 0) + 1
                    hour = f"{moment.hour:02d}"
                    by_bucket[hour] = by_bucket.get(hour, 0) + 1
                    for pos in engine.positions.open_positions():
                        open_meta[pos.position_id] = {
                            "opened_at": pos.opened_at,
                            "instrument": pos.contract_id,
                            "expiry": pos.expiry.isoformat(),
                            "regime": cycle.regime,
                        }
                        by_instrument[pos.contract_id] = by_instrument.get(pos.contract_id, 0) + 1
                        by_expiry[pos.expiry.isoformat()] = by_expiry.get(pos.expiry.isoformat(), 0) + 1
                        exposure = max(exposure, abs(pos.entry_price * pos.quantity))
                else:
                    rejections.append({"reason": result.reason, "as_of": moment.isoformat()})
                    if "DAILY_LOSS" in result.reason:
                        daily_loss_breaches += 1
                    if "RISK_GUARD" in result.reason or "PER_TRADE" in result.reason:
                        per_trade_risk_breaches += 1
                    if "NOT_APPROVED" in result.reason or "BLOCKED" in result.reason:
                        blocked += 1
                    if result.reason.startswith("DATA_"):
                        data_quality_failures += 1

            engine.on_snapshot(snap)
            for pos in engine.positions.all():
                if pos.state.value != "CLOSED" or pos.position_id in recorded_closed:
                    continue
                recorded_closed.add(pos.position_id)
                meta = open_meta.get(pos.position_id, {})
                # Prefer engine-reconciled net (gross − costs); stress scales research costs.
                gross = float(pos.realized_gross)
                cost = float(pos.total_costs) * self.cost_stress
                net = round(gross - cost, 4)
                opened = meta.get("opened_at") or pos.opened_at
                closed_at = pos.closed_at or moment
                if opened is not None:
                    durations.append((closed_at - opened).total_seconds())
                trades.append(
                    {
                        "position_id": pos.position_id,
                        "instrument": meta.get("instrument", pos.contract_id),
                        "expiry": meta.get("expiry", pos.expiry.isoformat()),
                        "gross_pnl": gross,
                        "total_cost": round(cost, 4),
                        "net_pnl": net,
                        "decision_as_of": opened.isoformat() if hasattr(opened, "isoformat") else str(opened),
                        "exit_day": closed_at.date().isoformat(),
                        "exit_reason": pos.exit_reason,
                        "fee_model_version": self.costs.version,
                        "slippage_model_version": self.policy.version,
                        "regime": meta.get("regime", cycle.regime),
                    }
                )
            equity.append(self._equity(engine))

        metrics = calculate_metrics(
            trades=trades,
            decisions=decisions,
            equity=equity,
            starting_cash=self.config.paper.starting_cash,
            daily_loss_breaches=daily_loss_breaches,
            per_trade_risk_breaches=per_trade_risk_breaches,
            blocked_count=blocked,
            data_quality_failures=data_quality_failures,
            exposure_notional=exposure,
            position_durations=durations,
            by_regime=by_regime,
            by_expiry=by_expiry,
            by_instrument=by_instrument,
            by_time_bucket=by_bucket,
        )
        return ReplayResult(
            role=role,
            metrics=metrics,
            trades=trades,
            decisions=decisions,
            rejections=rejections,
            leakage_flags=sorted(set(leakage)),
            cycle_count=len(selected),
            candidate_count=candidates,
            journal=engine.journal.to_list(),
            equity=equity,
        )

    @staticmethod
    def _equity(engine: PaperExecutionEngine) -> float:
        summary = engine.positions.summary()
        return round(engine.config.paper.starting_cash + summary.total_pnl, 4)


def filter_cycles(
    cycles: Sequence[HistoricalCycle],
    sessions: tuple[date, ...],
    start: date,
    end: date,
) -> tuple[HistoricalCycle, ...]:
    allowed = set(sessions_in_range(sessions, start, end))
    return tuple(c for c in cycles if c.day in allowed)
