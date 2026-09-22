"""Robustness analysis across walk-forward windows.

Compares all eligible windows (no cherry-picking), reports research→OOS
degradation, fee/slippage stress, regime splits, and concentration risk.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def degradation(research_net: float, oos_net: float) -> dict[str, Any]:
    delta = round(oos_net - research_net, 4)
    if research_net == 0:
        ratio = None
    else:
        ratio = round(oos_net / research_net, 4)
    return {
        "research_or_validation_net": research_net,
        "out_of_sample_net": oos_net,
        "delta": delta,
        "oos_to_research_ratio": ratio,
        "degraded": oos_net < research_net,
    }


def concentration(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "trade_count": 0,
            "top_trade_share_of_abs_pnl": None,
            "top_period_share": None,
            "dependent_on_few_trades": False,
        }
    abs_pnls = sorted((abs(float(t.get("net_pnl", 0))) for t in trades), reverse=True)
    total = sum(abs_pnls) or 1.0
    top1 = abs_pnls[0] / total
    by_day: dict[str, float] = {}
    for trade in trades:
        day = str(trade.get("exit_day") or trade.get("decision_as_of") or "")[:10]
        by_day[day] = by_day.get(day, 0.0) + abs(float(trade.get("net_pnl", 0)))
    period_share = (max(by_day.values()) / total) if by_day else None
    return {
        "trade_count": len(trades),
        "top_trade_share_of_abs_pnl": round(top1, 4),
        "top_period_share": None if period_share is None else round(period_share, 4),
        "dependent_on_few_trades": top1 >= 0.5 or (period_share is not None and period_share >= 0.5),
    }


def fee_slippage_matrix(
    base_net: float,
    *,
    cost_x2_net: float,
    slip_x2_net: float,
) -> list[dict[str, Any]]:
    return [
        {
            "name": "cost_x2",
            "base_net_pnl": base_net,
            "stressed_net_pnl": cost_x2_net,
            "delta": round(cost_x2_net - base_net, 4),
        },
        {
            "name": "slippage_x2",
            "base_net_pnl": base_net,
            "stressed_net_pnl": slip_x2_net,
            "delta": round(slip_x2_net - base_net, 4),
        },
    ]


def summarize_windows(
    window_metrics: Sequence[Mapping[str, Any]],
    *,
    retain_all: bool = True,
) -> dict[str, Any]:
    """Retain results across all eligible windows. Do not drop unfavorable ones."""

    if not retain_all:
        from grow.errors import GrowConfigError

        raise GrowConfigError("CHERRY_PICKING_FORBIDDEN")
    nets = [float(m.get("net_pnl", 0)) for m in window_metrics]
    samples = [int(m.get("sample_size", 0)) for m in window_metrics]
    return {
        "window_count": len(window_metrics),
        "retained_all_windows": True,
        "net_pnl_by_window": nets,
        "sample_size_by_window": samples,
        "combined_net_pnl": round(sum(nets), 4),
        "mean_net_pnl": round(sum(nets) / len(nets), 4) if nets else 0.0,
        "min_net_pnl": min(nets) if nets else 0.0,
        "max_net_pnl": max(nets) if nets else 0.0,
        "label": "WALK-FORWARD VALIDATION / NOT LIVE",
    }


def build_robustness_report(
    *,
    test_metrics: Sequence[Mapping[str, Any]],
    validation_metrics: Sequence[Mapping[str, Any]],
    test_trades: Sequence[Mapping[str, Any]],
    fee_stress: Sequence[Mapping[str, Any]] | None = None,
    regime_splits: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    val_net = round(sum(float(m.get("net_pnl", 0)) for m in validation_metrics), 4)
    test_net = round(sum(float(m.get("net_pnl", 0)) for m in test_metrics), 4)
    return {
        "windows": summarize_windows(test_metrics, retain_all=True),
        "degradation_validation_to_oos": degradation(val_net, test_net),
        "concentration": concentration(test_trades),
        "fee_slippage_variations": list(fee_stress or []),
        "regime_splits": dict(regime_splits or {}),
        "cherry_picked": False,
        "label": "WALK-FORWARD VALIDATION / NOT LIVE",
        "profitability_claim": False,
    }
