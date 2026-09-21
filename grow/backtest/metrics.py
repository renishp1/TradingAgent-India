"""Descriptive backtest metrics. Not proof of future profit."""

from __future__ import annotations

from grow.backtest.ledger import BacktestLedger
from grow.backtest.models import BacktestTrade


def _drawdown(equity: list[tuple[str, float]]) -> float:
    peak = equity[0][1] if equity else 0.0
    max_dd = 0.0
    for _, value in equity:
        peak = max(peak, value)
        max_dd = min(max_dd, value - peak)
    return round(max_dd, 4)


def calculate(ledger: BacktestLedger) -> dict:
    trades = ledger.primary_trades
    decisions = ledger.decisions
    wins = tuple(t for t in trades if t.net_pnl > 0)
    losses = tuple(t for t in trades if t.net_pnl < 0)
    gross = round(sum(t.gross_pnl for t in trades), 4)
    costs = round(sum(t.total_cost for t in trades), 4)
    net = round(sum(t.net_pnl for t in trades), 4)
    win_sum = sum(t.net_pnl for t in wins)
    loss_sum = abs(sum(t.net_pnl for t in losses))
    avg_win = (win_sum / len(wins)) if wins else 0.0
    avg_loss = (loss_sum / len(losses)) if losses else 0.0
    profit_factor = (win_sum / loss_sum) if loss_sum else (float("inf") if win_sum else 0.0)
    expectancy = (net / len(trades)) if trades else 0.0
    no_trade = sum(1 for d in decisions if d.status == "NO_TRADE")
    fills = sum(1 for d in decisions if d.status == "FILL")
    by_under: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for trade in trades:
        by_under[trade.underlying] = by_under.get(trade.underlying, 0) + 1
        by_type[trade.option_type] = by_type.get(trade.option_type, 0) + 1
    return {
        "trade_count": len(trades),
        "decision_count": len(decisions),
        "fill_count": fills,
        "no_trade_count": no_trade,
        "gross_pnl": gross,
        "total_cost": costs,
        "net_pnl": net,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "average_win": round(avg_win, 4),
        "average_loss": round(avg_loss, 4),
        "payoff_ratio": round(avg_win / avg_loss, 4) if avg_loss else 0.0,
        "profit_factor": profit_factor if profit_factor != float("inf") else None,
        "expectancy": round(expectancy, 4),
        "max_drawdown": _drawdown(ledger.equity),
        "ending_equity": ledger.cash,
        "by_underlying": by_under,
        "by_option_type": by_type,
        "sample_size": len(trades),
        "label": "HISTORICAL RESEARCH / NOT LIVE",
    }


def stress_note(base_net: float, stressed_net: float, name: str) -> dict:
    return {
        "name": name,
        "base_net_pnl": base_net,
        "stressed_net_pnl": stressed_net,
        "delta": round(stressed_net - base_net, 4),
        "label": "HISTORICAL RESEARCH / NOT LIVE",
    }
