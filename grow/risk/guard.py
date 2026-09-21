"""Deterministic Risk Guard.

The guard is code, not an LLM. It is the only component allowed to mint a
RiskStamp. The paper ledger will not accept an unstamped or forged proposal.

Rule evaluation is ordered and fail-closed: the first failing rule rejects.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime

from grow.clock import IST, Clock, SystemClock
from grow.config import GrowConfig
from grow.errors import GrowSafetyError
from grow.execution.lock import assert_paper_runtime
from grow.market.session import SessionCalendar
from grow.types import Intent, MarketBrief, RiskStamp, RiskVerdict, Side, TradeProposal, Venue

_RULESET_SECRET = os.environ.get("GROW_RISK_SECRET", "grow-risk-v1-paper-only")


class RiskGuard:
    def __init__(self, config: GrowConfig, clock: Clock | None = None) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self.calendar = SessionCalendar(config.market, clock=self.clock)

    def _stamp(self, proposal: TradeProposal, issued_at: datetime) -> RiskStamp:
        payload = (
            f"{proposal.proposal_id}|{proposal.symbol.qualified()}|{proposal.side.value}|"
            f"{proposal.intent.value}|{proposal.quantity}|{proposal.limit_price:.4f}|"
            f"{proposal.venue.value}|{self.config.risk.ruleset}"
        )
        token = hmac.new(
            _RULESET_SECRET.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return RiskStamp(
            proposal_id=proposal.proposal_id,
            issued_at=issued_at,
            ruleset=self.config.risk.ruleset,
            token=token,
        )

    def verify_stamp(self, proposal: TradeProposal, stamp: RiskStamp | None) -> bool:
        if stamp is None:
            return False
        expected = self._stamp(proposal, stamp.issued_at)
        return hmac.compare_digest(expected.token, stamp.token) and stamp.proposal_id == proposal.proposal_id

    def evaluate(
        self,
        proposal: TradeProposal,
        brief: MarketBrief,
        *,
        cash: float,
        gross_notional: float,
        daily_pnl: float,
        symbol_notional: float,
    ) -> RiskVerdict:
        assert_paper_runtime(
            self.config.execution.mode,
            self.config.execution.live_trading_enabled,
            proposal.venue.value,
        )
        now = self.clock.now().astimezone(IST)
        results: list[tuple[str, bool, str]] = []

        def rule(rule_id: str, passed: bool, detail: str) -> None:
            results.append((rule_id, passed, detail))

        rule(
            "venue.paper",
            proposal.venue is Venue.PAPER,
            f"venue={proposal.venue.value}",
        )
        rule(
            "symbol.universe",
            proposal.symbol.ticker in self.config.market.universe,
            f"{proposal.symbol.ticker} listed={proposal.symbol.ticker in self.config.market.universe}",
        )
        rule(
            "quantity.positive",
            proposal.quantity > 0,
            f"qty={proposal.quantity}",
        )
        rule(
            "price.positive",
            proposal.limit_price > 0 and brief.last_price > 0,
            f"limit={proposal.limit_price} last={brief.last_price}",
        )
        if self.config.risk.require_stop_loss and proposal.intent is Intent.OPEN:
            stop_ok = proposal.stop_loss is not None and proposal.stop_loss > 0
            if proposal.side is Side.BUY:
                stop_ok = stop_ok and proposal.stop_loss < proposal.limit_price
            else:
                stop_ok = stop_ok and proposal.stop_loss > proposal.limit_price
            rule("stop.required", bool(stop_ok), f"stop={proposal.stop_loss}")
        else:
            rule("stop.required", True, "not required for flatten")

        rule(
            "notional.position",
            proposal.notional <= self.config.risk.max_position_notional,
            f"{proposal.notional:.2f} <= {self.config.risk.max_position_notional:.2f}",
        )
        new_gross = gross_notional + (proposal.notional if proposal.intent is Intent.OPEN else 0.0)
        rule(
            "notional.gross",
            new_gross <= self.config.risk.max_gross_notional,
            f"{new_gross:.2f} <= {self.config.risk.max_gross_notional:.2f}",
        )
        rule(
            "cash.available",
            proposal.intent is not Intent.OPEN or proposal.notional <= cash,
            f"cash={cash:.2f} need={proposal.notional:.2f}",
        )
        rule(
            "loss.daily",
            daily_pnl >= -abs(self.config.risk.max_daily_loss),
            f"daily_pnl={daily_pnl:.2f} floor={-abs(self.config.risk.max_daily_loss):.2f}",
        )

        book_equity = max(cash + gross_notional, 1.0)
        concentration = (symbol_notional + (proposal.notional if proposal.intent is Intent.OPEN else 0.0)) / book_equity
        rule(
            "concentration.symbol",
            concentration <= self.config.risk.max_symbol_concentration + 1e-9,
            f"{concentration:.3f} <= {self.config.risk.max_symbol_concentration:.3f}",
        )

        session = self.calendar.state(now)
        if proposal.intent is Intent.OPEN:
            rule(
                "session.entries",
                self.calendar.allows_new_entries(now),
                f"session={session.value}",
            )
        else:
            flatten_ok = session.value in {"OPEN", "SQUARE_OFF_WINDOW"}
            rule("session.flatten", flatten_ok, f"session={session.value}")

        high_vol_block = brief.regime.value == "HIGH_VOLATILITY" and proposal.intent is Intent.OPEN
        rule("regime.volatility", not high_vol_block, f"regime={brief.regime.value}")

        failed = [(rid, detail) for rid, passed, detail in results if not passed]
        if failed:
            rule_id, detail = failed[0]
            return RiskVerdict(
                approved=False,
                rule_results=tuple(results),
                stamp=None,
                reason=f"{rule_id}: {detail}",
            )
        stamp = self._stamp(proposal, now)
        return RiskVerdict(
            approved=True,
            rule_results=tuple(results),
            stamp=stamp,
            reason="approved",
        )

    def require_stamp(self, proposal: TradeProposal, stamp: RiskStamp | None) -> None:
        if not self.verify_stamp(proposal, stamp):
            raise GrowSafetyError("Paper ledger refused an unstamped or forged proposal.")
