"""In-memory paper ledger.

The only venue Grow can fill. Every fill requires a RiskStamp that verifies
against the current Risk Guard. No network, no broker, no persistence yet.

Architecture Review #1: cash book is long-only. Negative quantity is a
safety error, not a short inventory. P&L is realized-at-cost; mark-to-market
is deferred until licensed quotes exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from grow.clock import Clock, SystemClock
from grow.config import GrowConfig
from grow.errors import GrowSafetyError
from grow.execution.lock import assert_paper_runtime
from grow.risk.guard import RiskGuard
from grow.types import Fill, Intent, RiskStamp, Side, Symbol, TradeProposal, Venue


@dataclass
class Position:
    symbol: Symbol
    quantity: int
    average_price: float

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise GrowSafetyError("Cash book refuses short inventory.")

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.average_price


@dataclass
class PaperBook:
    cash: float
    currency: str
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    realized_pnl: float = 0.0

    @property
    def gross_notional(self) -> float:
        return sum(pos.notional for pos in self.positions.values())

    def symbol_notional(self, ticker: str) -> float:
        pos = self.positions.get(ticker)
        return 0.0 if pos is None else pos.notional

    def snapshot(self) -> dict:
        return {
            "cash": self.cash,
            "currency": self.currency,
            "gross_notional": self.gross_notional,
            "realized_pnl": self.realized_pnl,
            "valuation": {
                "method": "cost_notional",
                "unrealized_pnl": None,
                "note": "MTM / drawdown deferred until licensed quotes. Do not treat realized_pnl as strategy performance.",
            },
            "positions": {
                key: {
                    "ticker": pos.symbol.ticker,
                    "quantity": pos.quantity,
                    "average_price": pos.average_price,
                    "notional": pos.notional,
                }
                for key, pos in self.positions.items()
            },
            "fills": [fill.to_dict() for fill in self.fills],
        }


class PaperLedger:
    def __init__(self, config: GrowConfig, guard: RiskGuard, clock: Clock | None = None) -> None:
        self.config = config
        self.guard = guard
        self.clock = clock or SystemClock()
        self.book = PaperBook(cash=config.paper.starting_cash, currency=config.market.currency)

    def submit(self, proposal: TradeProposal, stamp: RiskStamp | None) -> Fill:
        assert_paper_runtime(
            self.config.execution.mode,
            self.config.execution.live_trading_enabled,
            self.config.paper.venue_id,
        )
        if proposal.venue is not Venue.PAPER:
            raise GrowSafetyError("Paper ledger received a non-paper venue.")
        if proposal.intent is Intent.OPEN and proposal.side is Side.SELL and not self.config.risk.allow_short:
            raise GrowSafetyError("Cash book refuses to open a short.")
        self.guard.require_stamp(proposal, stamp)

        fill = Fill(
            fill_id=uuid4().hex[:12],
            proposal_id=proposal.proposal_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            price=proposal.limit_price,
            notional=proposal.notional,
            filled_at=self.clock.now(),
            venue_id=self.config.paper.venue_id,
        )
        self._apply(fill, proposal.intent)
        return fill

    def _apply(self, fill: Fill, intent: Intent) -> None:
        key = fill.symbol.ticker
        pos = self.book.positions.get(key)
        signed = fill.quantity if fill.side is Side.BUY else -fill.quantity

        if fill.side is Side.BUY:
            self.book.cash -= fill.notional
        else:
            self.book.cash += fill.notional

        if pos is None:
            if signed < 0:
                raise GrowSafetyError("Cash book refuses to open a short.")
            self.book.positions[key] = Position(symbol=fill.symbol, quantity=signed, average_price=fill.price)
        else:
            new_qty = pos.quantity + signed
            if new_qty < 0:
                raise GrowSafetyError("Cash book refuses short inventory.")
            if pos.quantity != 0 and (pos.quantity > 0) != (signed > 0):
                closed = min(abs(pos.quantity), abs(signed))
                direction = 1 if pos.quantity > 0 else -1
                self.book.realized_pnl += (fill.price - pos.average_price) * closed * direction
            if new_qty == 0:
                del self.book.positions[key]
            elif pos.quantity == 0 or (pos.quantity > 0) == (signed > 0):
                total = pos.average_price * abs(pos.quantity) + fill.price * abs(signed)
                pos.quantity = new_qty
                pos.average_price = total / abs(new_qty)
            else:
                pos.quantity = new_qty
                if abs(signed) > abs(pos.quantity):
                    pos.average_price = fill.price

        self.book.fills.append(fill)

    def square_off_proposal(self, ticker: str, last_price: float) -> TradeProposal | None:
        """Build a flatten proposal. Still requires Risk Guard before fill."""
        pos = self.book.positions.get(ticker)
        if pos is None or pos.quantity == 0:
            return None
        if pos.quantity < 0:
            raise GrowSafetyError("Cash book has illegal short inventory.")
        qty = abs(pos.quantity)
        return TradeProposal(
            proposal_id=f"sqoff-{ticker}-{uuid4().hex[:8]}",
            symbol=pos.symbol,
            side=Side.SELL,
            intent=Intent.SQUARE_OFF,
            quantity=qty,
            limit_price=last_price,
            stop_loss=None,
            take_profit=None,
            thesis="Forced intraday square-off window.",
            confidence=1.0,
            venue=Venue.PAPER,
            created_at=self.clock.now(),
            notional=round(qty * last_price, 2),
        )
