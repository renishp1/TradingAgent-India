"""3B paper position registry, MTM, and session P&L. Not a broker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from grow.clock import IST, Clock
from grow.live_data.models import LiveSnapshot
from grow.market.session import SessionCalendar
from grow.paper.exits import ExitDecision, ExitReason, choose_exit, session_close_due
from grow.paper.valuation import find_contract, mark_from_contract, quote_usable, unrealized_gross


class PositionState(str, Enum):
    OPEN = "OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    HALTED = "HALTED"


@dataclass
class PaperPosition:
    position_id: str
    session_id: str
    candidate_id: str | None
    contract_id: str
    underlying: str
    expiry: date
    strike: float
    option_type: str
    provider_id: str
    lot_size: int
    lots: int
    quantity: int
    entry_price: float
    opened_at: datetime
    stop_loss_price: float
    take_profit_price: float
    fill_id: str | None = None
    snapshot_id: str | None = None
    state: PositionState = PositionState.OPEN
    current_price: float | None = None
    last_valued_at: datetime | None = None
    price_source: str | None = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    realized_gross: float = 0.0
    total_costs: float = 0.0
    exit_reason: str | None = None
    closed_at: datetime | None = None
    close_fill_id: str | None = None
    close_snapshot_id: str | None = None
    diagnostics: list[str] = field(default_factory=list)

    def identity(self) -> tuple[str, str, float, str]:
        return (self.underlying, self.expiry.isoformat(), self.strike, self.option_type)

    def market_value(self) -> float | None:
        if self.state is not PositionState.OPEN or self.current_price is None:
            return None
        return round(self.current_price * self.quantity, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "session_id": self.session_id,
            "candidate_id": self.candidate_id,
            "contract_id": self.contract_id,
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type,
            "provider_id": self.provider_id,
            "lot_size": self.lot_size,
            "lots": self.lots,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "opened_at": self.opened_at.isoformat(),
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "state": self.state.value,
            "current_price": self.current_price,
            "last_valued_at": None if self.last_valued_at is None else self.last_valued_at.isoformat(),
            "price_source": self.price_source,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "realized_gross": self.realized_gross,
            "total_costs": self.total_costs,
            "exit_reason": self.exit_reason,
            "closed_at": None if self.closed_at is None else self.closed_at.isoformat(),
            "market_value": self.market_value(),
            "fill_id": self.fill_id,
            "snapshot_id": self.snapshot_id,
            "close_fill_id": self.close_fill_id,
            "close_snapshot_id": self.close_snapshot_id,
            "diagnostics": list(self.diagnostics),
            "live_trading": False,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PaperPosition:
        """Rebuild a recorded position. Used by paper restart, not by 3B marks."""

        def _dt(value: str | None) -> datetime | None:
            return None if value is None else datetime.fromisoformat(value)

        return cls(
            position_id=payload["position_id"],
            session_id=payload["session_id"],
            candidate_id=payload.get("candidate_id"),
            contract_id=payload["contract_id"],
            underlying=payload["underlying"],
            expiry=date.fromisoformat(payload["expiry"]),
            strike=float(payload["strike"]),
            option_type=payload["option_type"],
            provider_id=payload["provider_id"],
            lot_size=int(payload["lot_size"]),
            lots=int(payload["lots"]),
            quantity=int(payload["quantity"]),
            entry_price=float(payload["entry_price"]),
            opened_at=datetime.fromisoformat(payload["opened_at"]),
            stop_loss_price=float(payload["stop_loss_price"]),
            take_profit_price=float(payload["take_profit_price"]),
            fill_id=payload.get("fill_id"),
            snapshot_id=payload.get("snapshot_id"),
            state=PositionState(payload["state"]),
            current_price=None if payload.get("current_price") is None else float(payload["current_price"]),
            last_valued_at=_dt(payload.get("last_valued_at")),
            price_source=payload.get("price_source"),
            unrealized_pnl=float(payload.get("unrealized_pnl") or 0.0),
            realized_pnl=float(payload.get("realized_pnl") or 0.0),
            realized_gross=float(payload.get("realized_gross") or 0.0),
            total_costs=float(payload.get("total_costs") or 0.0),
            exit_reason=payload.get("exit_reason"),
            closed_at=_dt(payload.get("closed_at")),
            close_fill_id=payload.get("close_fill_id"),
            close_snapshot_id=payload.get("close_snapshot_id"),
            diagnostics=list(payload.get("diagnostics") or []),
        )


@dataclass(frozen=True)
class MarkEvent:
    position_id: str
    mark_price: float
    price_source: str
    unrealized_gross: float
    valuation_at: datetime
    snapshot_id: str | None
    diagnostic: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "mark_price": self.mark_price,
            "price_source": self.price_source,
            "unrealized_gross": self.unrealized_gross,
            "unrealized_net": self.unrealized_gross,
            "valuation_at": self.valuation_at.isoformat(),
            "snapshot_id": self.snapshot_id,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True)
class SessionSummary:
    starting_cash: float
    realized_pnl: float
    gross_realized_pnl: float
    total_costs: float
    net_realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    open_exposure: float
    opens: int
    closes: int
    stop_loss_exits: int
    take_profit_exits: int
    session_close_exits: int
    no_trade_count: int
    valuation_gaps: int
    halted: bool
    unresolved_close: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "starting_cash": self.starting_cash,
            "realized_pnl": self.realized_pnl,
            "gross_realized_pnl": self.gross_realized_pnl,
            "total_costs": self.total_costs,
            "net_realized_pnl": self.net_realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_pnl": self.total_pnl,
            "open_exposure": self.open_exposure,
            "opens": self.opens,
            "closes": self.closes,
            "stop_loss_exits": self.stop_loss_exits,
            "take_profit_exits": self.take_profit_exits,
            "session_close_exits": self.session_close_exits,
            "no_trade_count": self.no_trade_count,
            "valuation_gaps": self.valuation_gaps,
            "halted": self.halted,
            "unresolved_close": self.unresolved_close,
            "paper_only": True,
            "live_trading": False,
            "pnl_identity": "net_realized_pnl == gross_realized_pnl - total_costs",
        }


class PositionRegistry:
    def __init__(self, *, starting_cash: float, clock: Clock, calendar: SessionCalendar, session_close_square_off: bool) -> None:
        self.starting_cash = starting_cash
        self.clock = clock
        self.calendar = calendar
        self.session_close_square_off = session_close_square_off
        self._positions: dict[str, PaperPosition] = {}
        self._by_contract: dict[str, str] = {}
        self.marks: list[MarkEvent] = []
        self.events: list[dict[str, Any]] = []
        self.halted = False
        self.unresolved_close = False
        self.opens = 0
        self.closes = 0
        self.stop_loss_exits = 0
        self.take_profit_exits = 0
        self.session_close_exits = 0
        self.no_trade_count = 0
        self.valuation_gaps = 0

    def all(self) -> tuple[PaperPosition, ...]:
        return tuple(self._positions.values())

    def open_positions(self) -> tuple[PaperPosition, ...]:
        return tuple(p for p in self._positions.values() if p.state is PositionState.OPEN)

    def get(self, position_id: str) -> PaperPosition | None:
        return self._positions.get(position_id)

    def by_contract(self, contract_id: str) -> PaperPosition | None:
        pid = self._by_contract.get(contract_id)
        return None if pid is None else self._positions.get(pid)

    def has_open(self, contract_id: str) -> bool:
        pos = self.by_contract(contract_id)
        return pos is not None and pos.state is PositionState.OPEN

    def register_open(
        self,
        *,
        session_id: str,
        candidate_id: str | None,
        contract_id: str,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        provider_id: str,
        lot_size: int,
        lots: int,
        quantity: int,
        entry_price: float,
        opened_at: datetime,
        stop_loss_price: float,
        take_profit_price: float,
        fill_id: str | None,
        snapshot_id: str | None,
    ) -> PaperPosition:
        if lot_size < 1 or lots < 1 or quantity < 1:
            raise ValueError("OPEN quantity/lot_size must be positive")
        if quantity != lots * lot_size:
            raise ValueError("quantity must equal lots × lot_size")
        if option_type not in {"CE", "PE"}:
            raise ValueError("option_type must be CE or PE")
        if self.has_open(contract_id):
            raise ValueError("DUPLICATE_OPEN_POSITION")
        position = PaperPosition(
            position_id=f"pp-{uuid4().hex[:12]}",
            session_id=session_id,
            candidate_id=candidate_id,
            contract_id=contract_id,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            provider_id=provider_id,
            lot_size=lot_size,
            lots=lots,
            quantity=quantity,
            entry_price=entry_price,
            opened_at=opened_at,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            fill_id=fill_id,
            snapshot_id=snapshot_id,
        )
        self._positions[position.position_id] = position
        self._by_contract[contract_id] = position.position_id
        self.opens += 1
        self.events.append(
            {
                "type": "OPEN",
                "position_id": position.position_id,
                "session_id": session_id,
                "contract_id": contract_id,
                "state": PositionState.OPEN.value,
                "reason": "PAPER_OPEN",
                "provider_id": provider_id,
                "snapshot_id": snapshot_id,
                "timestamp": opened_at.isoformat(),
            }
        )
        return position

    def mark(self, snapshot: LiveSnapshot) -> list[MarkEvent]:
        events: list[MarkEvent] = []
        for position in self.open_positions():
            contract = find_contract(
                snapshot,
                underlying=position.underlying,
                expiry=position.expiry,
                strike=position.strike,
                option_type=position.option_type,
            )
            if contract is None:
                position.diagnostics.append("MISSING_QUOTE")
                self.valuation_gaps += 1
                self.events.append(
                    {
                        "type": "SAFETY",
                        "position_id": position.position_id,
                        "reason": "MISSING_QUOTE",
                        "snapshot_id": snapshot.snapshot_id,
                        "timestamp": snapshot.event_time.isoformat(),
                    }
                )
                continue
            why = quote_usable(contract, last_valued_at=position.last_valued_at)
            if why:
                position.diagnostics.append(why)
                continue
            priced = mark_from_contract(contract)
            if priced is None:
                position.diagnostics.append("MISSING_QUOTE")
                self.valuation_gaps += 1
                self.events.append(
                    {
                        "type": "SAFETY",
                        "position_id": position.position_id,
                        "reason": "MISSING_QUOTE",
                        "snapshot_id": snapshot.snapshot_id,
                        "timestamp": snapshot.event_time.isoformat(),
                    }
                )
                continue
            price, source = priced
            pnl = unrealized_gross(mark_price=price, entry_price=position.entry_price, quantity=position.quantity)
            position.current_price = price
            position.price_source = source
            position.last_valued_at = contract.timestamp
            position.unrealized_pnl = pnl
            event = MarkEvent(
                position_id=position.position_id,
                mark_price=price,
                price_source=source,
                unrealized_gross=pnl,
                valuation_at=contract.timestamp,
                snapshot_id=snapshot.snapshot_id,
            )
            self.marks.append(event)
            events.append(event)
        return events

    def exits(self, snapshot: LiveSnapshot) -> list[ExitDecision]:
        due = session_close_due(self.calendar, snapshot.event_time, enabled=self.session_close_square_off)
        decisions: list[ExitDecision] = []
        unresolved = False
        for position in self.open_positions():
            if position.current_price is None or position.price_source is None:
                if due:
                    unresolved = True
                    position.diagnostics.append("UNRESOLVED_CLOSE")
                    self.events.append(
                        {
                            "type": "SAFETY",
                            "position_id": position.position_id,
                            "reason": "UNRESOLVED_CLOSE",
                            "snapshot_id": snapshot.snapshot_id,
                            "timestamp": snapshot.event_time.isoformat(),
                        }
                    )
                continue
            reason = choose_exit(
                mark_price=position.current_price,
                stop_loss_price=position.stop_loss_price,
                take_profit_price=position.take_profit_price,
                session_close=due,
            )
            if reason is None:
                continue
            decisions.append(
                ExitDecision(
                    position_id=position.position_id,
                    reason=reason,
                    mark_price=position.current_price,
                    price_source=position.price_source,
                )
            )
        if unresolved:
            self.unresolved_close = True
            self.halted = True
        return decisions

    def begin_exit(self, position_id: str) -> PaperPosition | None:
        position = self._positions.get(position_id)
        if position is None or position.state is PositionState.CLOSED:
            return None
        position.state = PositionState.EXIT_PENDING
        return position

    def abort_exit(self, position_id: str, reason: str) -> None:
        position = self._positions.get(position_id)
        if position is None or position.state is PositionState.CLOSED:
            return
        position.state = PositionState.OPEN
        position.diagnostics.append(reason)
        self.events.append(
            {
                "type": "SAFETY",
                "position_id": position_id,
                "reason": reason,
                "timestamp": self.clock.now().isoformat(),
            }
        )

    def complete_close(
        self,
        position_id: str,
        *,
        exit_reason: str,
        exit_price: float,
        closed_at: datetime,
        costs: float,
        close_fill_id: str | None,
        snapshot_id: str | None,
    ) -> PaperPosition:
        position = self._positions[position_id]
        if position.state is PositionState.CLOSED:
            raise ValueError("DUPLICATE_CLOSE")
        gross = round((exit_price - position.entry_price) * position.quantity, 4)
        net = round(gross - costs, 4)
        position.state = PositionState.CLOSED
        position.current_price = exit_price
        position.unrealized_pnl = 0.0
        position.realized_gross = gross
        position.realized_pnl = net
        position.total_costs = costs
        position.exit_reason = exit_reason
        position.closed_at = closed_at
        position.close_fill_id = close_fill_id
        position.close_snapshot_id = snapshot_id
        self.closes += 1
        if exit_reason == ExitReason.STOP_LOSS:
            self.stop_loss_exits += 1
        elif exit_reason == ExitReason.TAKE_PROFIT:
            self.take_profit_exits += 1
        elif exit_reason == ExitReason.SESSION_CLOSE:
            self.session_close_exits += 1
        self.events.append(
            {
                "type": "CLOSE",
                "position_id": position.position_id,
                "session_id": position.session_id,
                "contract_id": position.contract_id,
                "state": PositionState.CLOSED.value,
                "reason": exit_reason,
                "exit_price": exit_price,
                "quantity": position.quantity,
                "gross_pnl": gross,
                "costs": costs,
                "net_pnl": net,
                "closed_at": closed_at.isoformat(),
                "snapshot_id": snapshot_id,
                "provider_id": position.provider_id,
            }
        )
        return position

    def halt_for_timeout(self) -> None:
        """Fail closed on session timeout with inventory. Never fabricate a close price."""
        self.halted = True
        self.unresolved_close = True
        for position in self.open_positions():
            position.diagnostics.append("SESSION_TIMEOUT_WITH_OPEN_POSITION")
            self.events.append(
                {
                    "type": "SAFETY",
                    "position_id": position.position_id,
                    "session_id": position.session_id,
                    "contract_id": position.contract_id,
                    "state": position.state.value,
                    "reason": "SESSION_TIMEOUT_WITH_OPEN_POSITION",
                    "provider_id": position.provider_id,
                    "snapshot_id": position.snapshot_id,
                    "timestamp": self.clock.now().isoformat(),
                    "current_price": position.current_price,
                    "last_valued_at": None if position.last_valued_at is None else position.last_valued_at.isoformat(),
                }
            )

    def summary(self) -> SessionSummary:
        closed = tuple(p for p in self._positions.values() if p.state is PositionState.CLOSED)
        unrealized = round(sum(p.unrealized_pnl for p in self.open_positions()), 4)
        gross = round(sum(p.realized_gross for p in closed), 4)
        costs = round(sum(p.total_costs for p in closed), 4)
        net = round(sum(p.realized_pnl for p in closed), 4)
        exposure = 0.0
        gaps = 0
        for pos in self.open_positions():
            value = pos.market_value()
            if value is None:
                gaps += 1
            else:
                exposure += value
        return SessionSummary(
            starting_cash=self.starting_cash,
            realized_pnl=net,
            gross_realized_pnl=gross,
            total_costs=costs,
            net_realized_pnl=net,
            unrealized_pnl=unrealized,
            total_pnl=round(net + unrealized, 4),
            open_exposure=round(exposure, 4),
            opens=self.opens,
            closes=self.closes,
            stop_loss_exits=self.stop_loss_exits,
            take_profit_exits=self.take_profit_exits,
            session_close_exits=self.session_close_exits,
            no_trade_count=self.no_trade_count,
            valuation_gaps=self.valuation_gaps + gaps,
            halted=self.halted,
            unresolved_close=self.unresolved_close,
        )

    def trading_day_realized_pnl(self, moment: datetime) -> float:
        """Net realized P&L for closes on the IST calendar day of ``moment`` only."""
        day = moment.astimezone(IST).date()
        total = 0.0
        for pos in self._positions.values():
            if pos.state is not PositionState.CLOSED or pos.closed_at is None:
                continue
            if pos.closed_at.astimezone(IST).date() != day:
                continue
            total += pos.realized_pnl
        return round(total, 4)

    def trading_day_total_pnl(self, moment: datetime) -> float:
        """Trading-day realized + current unrealized (open marks are attributed to today)."""
        unrealized = round(sum(p.unrealized_pnl for p in self.open_positions()), 4)
        return round(self.trading_day_realized_pnl(moment) + unrealized, 4)

    def note_no_trade(self) -> None:
        self.no_trade_count += 1

    def adopt(self, position: PaperPosition) -> PaperPosition:
        """Restore a previously recorded position without minting a new id."""
        if position.position_id in self._positions:
            raise ValueError("DUPLICATE_POSITION")
        if position.state is PositionState.OPEN and self.has_open(position.contract_id):
            raise ValueError("DUPLICATE_OPEN_POSITION")
        self._positions[position.position_id] = position
        if position.state is PositionState.OPEN:
            self._by_contract[position.contract_id] = position.position_id
        return position
