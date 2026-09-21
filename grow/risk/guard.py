"""Deterministic Risk Guard.

The guard is code, not an LLM. It is the only component allowed to mint a
RiskStamp. The paper ledger will not accept an unstamped or forged proposal.

HMAC construction does **not** live here. `_stamp()` calls
`grow.risk.stamp.stamp_token` (`grow.risk.stamp.v2`: canonical JSON → SHA-256
→ HMAC-SHA256). The secret comes from `grow.risk.secret.resolve_risk_secret`.
There is no in-module HMAC payload and no published default.

Rule evaluation is ordered and fail-closed: the first failing rule rejects.
"""

from __future__ import annotations

import hmac
from datetime import datetime

from grow.clock import IST, Clock, SystemClock
from grow.config import GrowConfig
from grow.errors import GrowSafetyError
from grow.execution.lock import assert_paper_runtime
from grow.market.session import SessionCalendar
from grow.risk.secret import resolve_risk_secret
from grow.risk.stamp import stamp_token
from grow.types import Intent, MarketBrief, RiskStamp, RiskVerdict, Side, TradeProposal, Venue


class RiskGuard:
    def __init__(
        self,
        config: GrowConfig,
        clock: Clock | None = None,
        *,
        secret: str | None = None,
    ) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self.calendar = SessionCalendar(config.market, clock=self.clock)
        self._secret = resolve_risk_secret(secret)

    def _stamp(self, proposal: TradeProposal, issued_at: datetime) -> RiskStamp:
        return RiskStamp(
            proposal_id=proposal.proposal_id,
            issued_at=issued_at,
            ruleset=self.config.risk.ruleset,
            token=stamp_token(proposal, self.config.risk.ruleset, self._secret),
        )

    def verify_stamp(self, proposal: TradeProposal, stamp: RiskStamp | None) -> bool:
        if stamp is None:
            return False
        if stamp.ruleset != self.config.risk.ruleset:
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

        expected_notional = round(proposal.quantity * proposal.limit_price, 2)
        rule(
            "notional.matches",
            abs(proposal.notional - expected_notional) <= 1e-6,
            f"notional={proposal.notional:.2f} expected={expected_notional:.2f}",
        )

        opening_short = proposal.intent is Intent.OPEN and proposal.side is Side.SELL
        rule(
            "policy.long_only",
            self.config.risk.allow_short or not opening_short,
            "cash book: OPEN+SELL is a short and is forbidden",
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
        # `daily_pnl` is currently lifetime realized-at-cost of the in-memory
        # book, not a trading-day accumulator. See docs/safety.md.
        rule(
            "loss.daily",
            daily_pnl >= -abs(self.config.risk.max_daily_loss),
            f"daily_pnl={daily_pnl:.2f} floor={-abs(self.config.risk.max_daily_loss):.2f} (lifetime realized-at-cost until daily accumulator)",
        )

        # Review #1: this is cost-notional, not mark-to-market equity.
        # MTM equity is deferred until licensed quotes exist.
        book_equity = max(cash + gross_notional, 1.0)
        concentration = (symbol_notional + (proposal.notional if proposal.intent is Intent.OPEN else 0.0)) / book_equity
        rule(
            "concentration.symbol",
            concentration <= self.config.risk.max_symbol_concentration + 1e-9,
            (
                f"{concentration:.3f} <= {self.config.risk.max_symbol_concentration:.3f} "
                f"basis={self.config.risk.concentration_basis}"
            ),
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
