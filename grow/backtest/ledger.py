"""Backtest-only ledger. Isolated from the production paper book."""

from __future__ import annotations

from grow.backtest.models import BacktestTrade, DecisionRow


class BacktestLedger:
    def __init__(self, starting_cash: float = 1_000_000.0) -> None:
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.trades: list[BacktestTrade] = []
        self.decisions: list[DecisionRow] = []
        self.equity: list[tuple[str, float]] = [("start", starting_cash)]

    def record_decision(self, row: DecisionRow) -> None:
        self.decisions.append(row)

    def record_trade(self, trade: BacktestTrade) -> None:
        self.trades.append(trade)
        if trade.primary:
            self.cash = round(self.cash + trade.net_pnl, 4)
            self.equity.append((trade.exit_timestamp.isoformat(), self.cash))

    @property
    def primary_trades(self) -> tuple[BacktestTrade, ...]:
        return tuple(t for t in self.trades if t.primary)
