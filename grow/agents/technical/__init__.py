"""Technical Analysis Agent — deterministic indicators from the shared snapshot."""

from __future__ import annotations

from grow.agents.common import make_result, no_data, require_quality, timed_ms
from grow.decision.contracts.agent_result import AgentInput, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot, UnderlyingQuoteView
from grow.strategies.indicators import ema, rsi, sma


INDICATOR_CONFIG_VERSION = "technical.indicators.v1"


class TechnicalAgent:
    agent_name = "technical"
    agent_version = "technical.v2"

    def analyze(self, snapshot: AgentMarketSnapshot, *, cycle_id: str = ""):
        return timed_ms(lambda: self._analyze(snapshot, cycle_id=cycle_id))

    def analyze_input(self, agent_input: AgentInput):
        return self.analyze(agent_input.snapshot, cycle_id=agent_input.cycle_id)

    def _analyze(self, snapshot: AgentMarketSnapshot, *, cycle_id: str = ""):
        blocked = require_quality(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
            cycle_id=cycle_id,
        )
        if blocked is not None:
            return blocked
        if not snapshot.underlyings:
            return no_data(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=("underlyings",),
                cycle_id=cycle_id,
            )
        first = next(iter(snapshot.underlyings.values()))
        missing = _missing_ohlc(first)
        if missing:
            return no_data(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                missing=missing,
                cycle_id=cycle_id,
            )

        closes = _history_closes(snapshot, first)
        metrics: dict[str, float | str | None] = {
            "ltp": first.ltp,
            "high": first.high,
            "low": first.low,
            "indicator_config": INDICATOR_CONFIG_VERSION,
            "history_bars": len(closes),
        }
        range_pct = 0.0
        if first.ltp and first.high is not None and first.low is not None and first.ltp > 0:
            range_pct = round((first.high - first.low) / first.ltp, 6)
        metrics["range_pct"] = range_pct

        findings: list[str] = []
        interpretation: list[str] = []
        assumptions = (
            "Indicators use only closes present in the shared snapshot/history window.",
            f"indicator_config={INDICATOR_CONFIG_VERSION}",
        )

        if len(closes) < 14:
            return make_result(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                status=AgentStatus.DEGRADED,
                observations=(
                    f"underlying={first.underlying}",
                    f"ltp={first.ltp}",
                    f"history_bars={len(closes)}",
                ),
                calculated_metrics=metrics,
                interpretation=(
                    "Insufficient history for SMA/EMA/RSI; only single-window range calculated.",
                ),
                findings=("INSUFFICIENT_HISTORY",),
                assumptions=assumptions,
                evidence=(f"snapshot_id={snapshot.snapshot_id}", f"version={snapshot.version}"),
                metrics_used=("ltp", "high", "low", "range_pct", "history_bars"),
                candidate_action=CandidateAction.NONE,
                candidate_instrument=first.underlying,
                invalidation_reason="insufficient history for full indicator set",
                data_quality_concerns=("INSUFFICIENT_HISTORY",),
                cycle_id=cycle_id,
                confidence=0.3,
            )

        sma_fast = sma(closes, 5)
        sma_slow = sma(closes, 14)
        ema_fast = ema(closes, 5)
        rsi_14 = rsi(closes, 14)
        metrics.update(
            {
                "sma_5": sma_fast,
                "sma_14": sma_slow,
                "ema_5": ema_fast,
                "rsi_14": rsi_14,
            }
        )
        if sma_fast is not None and sma_slow is not None:
            if sma_fast > sma_slow:
                findings.append("SMA_FAST_ABOVE_SLOW")
                interpretation.append("Short SMA above long SMA — bullish structure candidate.")
            elif sma_fast < sma_slow:
                findings.append("SMA_FAST_BELOW_SLOW")
                interpretation.append("Short SMA below long SMA — bearish structure candidate.")
        if rsi_14 is not None:
            if rsi_14 >= 70:
                findings.append("RSI_OVERBOUGHT")
            elif rsi_14 <= 30:
                findings.append("RSI_OVERSOLD")

        return make_result(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
            status=AgentStatus.PASS,
            observations=(
                f"underlying={first.underlying}",
                f"ltp={first.ltp}",
                f"range_pct={range_pct:.4f}",
                f"history_bars={len(closes)}",
            ),
            calculated_metrics=metrics,
            interpretation=tuple(interpretation)
            or ("Indicators calculated; no directional trade recommendation emitted.",),
            findings=tuple(findings) or ("TECHNICAL_NEUTRAL",),
            assumptions=assumptions,
            evidence=(
                f"snapshot_id={snapshot.snapshot_id}",
                f"version={snapshot.version}",
                f"indicator_config={INDICATOR_CONFIG_VERSION}",
            ),
            metrics_used=("ltp", "high", "low", "range_pct", "sma_5", "sma_14", "ema_5", "rsi_14"),
            candidate_action=CandidateAction.NONE,
            candidate_instrument=first.underlying,
            invalidation_reason="technical findings are research-only",
            cycle_id=cycle_id,
            confidence=0.55,
        )


def _missing_ohlc(quote: UnderlyingQuoteView) -> tuple[str, ...]:
    missing: list[str] = []
    if quote.ltp is None and quote.spot is None:
        missing.append("ltp")
    if quote.high is None:
        missing.append("high")
    if quote.low is None:
        missing.append("low")
    return tuple(missing)


def _history_closes(snapshot: AgentMarketSnapshot, quote: UnderlyingQuoteView) -> tuple[float, ...]:
    raw = snapshot.diagnostics.get("history_closes") if snapshot.diagnostics else None
    if isinstance(raw, (list, tuple)) and raw:
        closes = tuple(float(v) for v in raw)
        return closes
    # Fall back to what the point-in-time quote provides — never invent bars.
    values: list[float] = []
    for value in (quote.open, quote.high, quote.low, quote.close, quote.ltp or quote.spot):
        if value is not None:
            values.append(float(value))
    return tuple(values)
