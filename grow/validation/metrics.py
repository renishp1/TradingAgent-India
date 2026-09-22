"""Walk-forward metrics with explicit sample sizes.

Descriptive only — not a claim of future profitability.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _drawdown(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        max_dd = min(max_dd, value - peak)
    return round(max_dd, 4)


def _volatility(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return round(var**0.5, 6)


def calculate_metrics(
    *,
    trades: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    equity: Sequence[float],
    starting_cash: float,
    daily_loss_breaches: int = 0,
    per_trade_risk_breaches: int = 0,
    blocked_count: int = 0,
    data_quality_failures: int = 0,
    exposure_notional: float = 0.0,
    position_durations: Sequence[float] = (),
    by_regime: Mapping[str, int] | None = None,
    by_expiry: Mapping[str, int] | None = None,
    by_instrument: Mapping[str, int] | None = None,
    by_time_bucket: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    wins = [t for t in trades if float(t.get("net_pnl", 0)) > 0]
    losses = [t for t in trades if float(t.get("net_pnl", 0)) < 0]
    gross = round(sum(float(t.get("gross_pnl", 0)) for t in trades), 4)
    costs = round(sum(float(t.get("total_cost", t.get("costs", 0))) for t in trades), 4)
    net = round(sum(float(t.get("net_pnl", 0)) for t in trades), 4)
    win_sum = sum(float(t["net_pnl"]) for t in wins)
    loss_sum = abs(sum(float(t["net_pnl"]) for t in losses))
    avg_win = (win_sum / len(wins)) if wins else 0.0
    avg_loss = (loss_sum / len(losses)) if losses else 0.0
    profit_factor = (win_sum / loss_sum) if loss_sum else (None if not win_sum else None)
    if loss_sum and win_sum:
        profit_factor = round(win_sum / loss_sum, 4)
    elif win_sum and not loss_sum:
        profit_factor = None  # undefined / infinite — do not fabricate
    no_trade = sum(1 for d in decisions if d.get("status") in {"NO_TRADE", "no_trade"})
    executed = sum(1 for d in decisions if d.get("status") in {"FILL", "FILLED", "EXECUTED", "CANDIDATE_FILLED"})
    decision_n = len(decisions)
    trade_n = len(trades)
    returns = []
    prev = starting_cash
    for value in equity[1:] if equity else []:
        if prev:
            returns.append((value - prev) / prev)
        prev = value
    avg_duration = (sum(position_durations) / len(position_durations)) if position_durations else 0.0
    sample_size = trade_n
    evidence_strength = "WEAK" if sample_size < 30 else ("MODERATE" if sample_size < 100 else "STRONG")
    return {
        "trade_count": trade_n,
        "executed_paper_trades": trade_n,
        "decision_count": decision_n,
        "fill_count": executed,
        "no_trade_count": no_trade,
        "no_trade_rate": round(no_trade / decision_n, 4) if decision_n else 0.0,
        "blocked_trade_count": blocked_count,
        "blocked_trade_rate": round(blocked_count / decision_n, 4) if decision_n else 0.0,
        "gross_pnl": gross,
        "total_cost": costs,
        "net_pnl": net,
        "win_count": len(wins),
        "loss_count": len(losses),
        "average_win": round(avg_win, 4),
        "average_loss": round(avg_loss, 4),
        "profit_factor": profit_factor,
        "max_drawdown": _drawdown(list(equity) if equity else [starting_cash]),
        "daily_loss_limit_breaches": daily_loss_breaches,
        "per_trade_risk_breaches": per_trade_risk_breaches,
        "exposure_notional": round(exposure_notional, 4),
        "average_position_duration_seconds": round(avg_duration, 4),
        "return_volatility": _volatility(returns),
        "data_quality_failure_rate": round(data_quality_failures / decision_n, 4) if decision_n else 0.0,
        "data_quality_failures": data_quality_failures,
        "by_regime": dict(by_regime or {}),
        "by_expiry": dict(by_expiry or {}),
        "by_instrument": dict(by_instrument or {}),
        "by_time_bucket": dict(by_time_bucket or {}),
        "sample_size": sample_size,
        "evidence_strength": evidence_strength,
        "ending_equity": equity[-1] if equity else starting_cash,
        "label": "WALK-FORWARD VALIDATION / NOT LIVE",
        "profitability_claim": False,
    }
