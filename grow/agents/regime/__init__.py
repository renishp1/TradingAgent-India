"""Market Regime Agent — reproducible environment labels with evidence.

Direction uses same-period movement only. Prefer LIVE M15 ``history_closes``
when present. Never treat Kite quote ``ohlc.close`` (prior-day close) as the
same-session close versus today's ``ohlc.open``.

Multi-index: evaluate each underlying independently; emit TRENDING_UP/DOWN
only when all evaluated underlyings agree (avoids DIRECTION_CONFLICT).
"""

from __future__ import annotations

from grow.agents.common import make_result, no_data, require_quality, timed_ms
from grow.decision.contracts.agent_result import AgentInput, AgentStatus, CandidateAction
from grow.market_data.normalized.models import AgentMarketSnapshot, UnderlyingQuoteView

# ~one NSE/BSE cash session of M15 bars (09:15–15:30). Caps multi-day history windows.
_REGIME_M15_SESSION_BARS = 26


class RegimeAgent:
    agent_name = "regime"
    agent_version = "regime.v4"

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
        labels: dict[str, str] = {}
        sources: dict[str, str] = {}
        metrics: dict[str, float | str | int | None] = {
            "underlyings_evaluated": len(snapshot.underlyings),
        }
        assumptions = (
            "Same-period features only; prefers M15 history_closes when present.",
            "Never uses Kite prior-day ohlc.close as session close.",
            "Labels are candidates, not certainties.",
            "Multi-index: TRENDING_UP/DOWN only when all underlyings agree.",
        )

        for name, quote in snapshot.underlyings.items():
            ltp = quote.ltp if quote.ltp is not None else quote.spot
            if ltp is None or ltp <= 0:
                metrics[f"{name}_regime"] = "NO_LTP"
                continue
            period = _same_period_levels(snapshot, quote, float(ltp))
            if period is None:
                metrics[f"{name}_regime"] = "INSUFFICIENT"
                continue
            period_open, period_close, range_high, range_low, source, history_bars = period
            range_pct = (range_high - range_low) / float(ltp) if float(ltp) > 0 else 0.0
            metrics[f"{name}_range_pct"] = round(range_pct, 6)
            metrics[f"{name}_period_open"] = period_open
            metrics[f"{name}_period_close"] = period_close
            metrics[f"{name}_history_bars"] = history_bars
            metrics[f"{name}_regime_source"] = source
            sources[name] = source
            if range_pct >= 0.02:
                labels[name] = "HIGH_VOLATILITY"
            elif period_close > period_open:
                labels[name] = "TRENDING_UP_CANDIDATE"
            elif period_close < period_open:
                labels[name] = "TRENDING_DOWN_CANDIDATE"
            else:
                labels[name] = "RANGING_CANDIDATE"
            metrics[f"{name}_regime"] = labels[name]

        if not labels:
            return make_result(
                agent_name=self.agent_name,
                agent_version=self.agent_version,
                snapshot=snapshot,
                status=AgentStatus.DEGRADED,
                observations=(
                    f"underlying={first.underlying}",
                    "regime_source=INSUFFICIENT",
                ),
                calculated_metrics={
                    "regime_label": "UNKNOWN",
                    "regime_source": "INSUFFICIENT",
                    "history_bars": _history_bar_count(snapshot, first),
                    **metrics,
                },
                interpretation=(
                    "Same-period open/close unavailable; regime cannot be classified.",
                ),
                findings=("INSUFFICIENT_DATA", "UNKNOWN"),
                assumptions=assumptions,
                evidence=(
                    f"snapshot_id={snapshot.snapshot_id}",
                    f"version={snapshot.version}",
                ),
                metrics_used=("ltp", "history_closes"),
                candidate_action=CandidateAction.ABSTAIN,
                candidate_instrument=first.underlying,
                entry_reason="regime=UNKNOWN",
                invalidation_reason="regime labels are provisional and not trade orders",
                data_quality_concerns=("INSUFFICIENT_DATA",),
                cycle_id=cycle_id,
                confidence=0.2,
            )

        unique = set(labels.values())
        # Primary (first) metrics for backward-compatible consumers.
        primary_name = first.underlying
        primary_label = labels.get(primary_name) or next(iter(labels.values()))
        primary_source = sources.get(primary_name) or next(iter(sources.values()), "INSUFFICIENT")
        metrics["regime_label"] = primary_label
        metrics["regime_source"] = primary_source
        metrics["history_bars"] = metrics.get(f"{primary_name}_history_bars") or _history_bar_count(
            snapshot, first
        )
        if f"{primary_name}_range_pct" in metrics:
            metrics["range_pct"] = metrics[f"{primary_name}_range_pct"]
            metrics["period_open"] = metrics.get(f"{primary_name}_period_open")
            metrics["period_close"] = metrics.get(f"{primary_name}_period_close")

        if len(unique) == 1:
            label = next(iter(unique))
            findings = (label,)
            if label == "HIGH_VOLATILITY":
                interpretation = ("Wide same-period range relative to LTP suggests elevated volatility.",)
                confidence = 0.6
            elif label == "TRENDING_UP_CANDIDATE":
                interpretation = (
                    "Same-period close above open with modest range — provisional uptrend candidate.",
                )
                confidence = 0.45
            elif label == "TRENDING_DOWN_CANDIDATE":
                interpretation = (
                    "Same-period close below open with modest range — provisional downtrend candidate.",
                )
                confidence = 0.45
            else:
                interpretation = (
                    "Flat same-period open/close with modest range — provisional ranging label.",
                )
                confidence = 0.4
            metrics["regime_label"] = label
        else:
            # Mixed underlyings: do not emit TRENDING_UP and TRENDING_DOWN together.
            label = "MULTI_INDEX_MIXED"
            findings = ("MULTI_INDEX_MIXED", "RANGING_CANDIDATE")
            interpretation = (
                "Underlyings disagree on regime direction; no single TRENDING_UP/DOWN signal.",
            )
            confidence = 0.35
            metrics["regime_label"] = label

        ltp0 = first.ltp if first.ltp is not None else first.spot
        return make_result(
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            snapshot=snapshot,
            status=AgentStatus.PASS,
            observations=(
                f"underlyings={','.join(snapshot.underlyings.keys())}",
                f"ltp={ltp0}",
                f"regime_label={metrics.get('regime_label')}",
                f"regime_source={primary_source}",
            ),
            calculated_metrics=metrics,
            interpretation=interpretation,
            findings=findings,
            assumptions=assumptions,
            evidence=(
                f"snapshot_id={snapshot.snapshot_id}",
                f"version={snapshot.version}",
                f"regime_source={primary_source}",
            ),
            metrics_used=("range_pct", "period_open", "period_close", "history_closes"),
            candidate_action=CandidateAction.ABSTAIN,
            candidate_instrument=",".join(snapshot.underlyings.keys()) or first.underlying,
            entry_reason=f"regime={metrics.get('regime_label')}",
            invalidation_reason="regime labels are provisional and not trade orders",
            cycle_id=cycle_id,
            confidence=confidence,
        )


def _history_closes_for(
    snapshot: AgentMarketSnapshot,
    quote: UnderlyingQuoteView | None = None,
) -> tuple[float, ...]:
    from collections.abc import Mapping

    name = str(quote.underlying or "").strip().upper() if quote is not None else ""
    by = snapshot.diagnostics.get("history_closes_by_underlying") if snapshot.diagnostics else None
    if isinstance(by, Mapping) and name and name in by and by[name]:
        return tuple(float(v) for v in by[name])
    raw = snapshot.diagnostics.get("history_closes") if snapshot.diagnostics else None
    if isinstance(raw, (list, tuple)) and raw:
        if quote is None or len(snapshot.underlyings) <= 1:
            return tuple(float(v) for v in raw)
    return ()


def _history_closes(snapshot: AgentMarketSnapshot) -> tuple[float, ...]:
    return _history_closes_for(snapshot, None)


def _history_bar_count(
    snapshot: AgentMarketSnapshot,
    quote: UnderlyingQuoteView | None = None,
) -> int:
    return len(_history_closes_for(snapshot, quote))


def _same_period_levels(
    snapshot: AgentMarketSnapshot,
    quote: UnderlyingQuoteView,
    ltp: float,
) -> tuple[float, float, float, float, str, int] | None:
    """Return (open, close, high, low, source, history_bars) for one period.

    Prefer M15 closes. Fallback compares today's open to LTP — never quote.close
    (Kite prior-day close).
    """
    closes = _history_closes_for(snapshot, quote)
    if len(closes) >= 2:
        window = closes[-_REGIME_M15_SESSION_BARS:] if len(closes) > _REGIME_M15_SESSION_BARS else closes
        period_open = float(window[0])
        period_close = float(window[-1])
        return (
            period_open,
            period_close,
            max(window),
            min(window),
            "M15_HISTORY",
            len(closes),
        )

    # Same-session fallback: today's open vs current LTP (both intraday).
    if quote.open is not None and float(quote.open) > 0:
        open_px = float(quote.open)
        high = float(quote.high) if quote.high is not None else max(open_px, ltp)
        low = float(quote.low) if quote.low is not None else min(open_px, ltp)
        return (open_px, ltp, high, low, "LTP_VS_TODAY_OPEN", len(closes))

    return None


__all__ = ["RegimeAgent"]
