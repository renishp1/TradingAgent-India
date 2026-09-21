from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta
from dataclasses import replace

from grow.clock import IST
from grow.config import load_config
from grow.data.boundary import research_view
from grow.data.schema import FIXTURE_SOURCE, Bar, BarSeries, MarketSnapshot, SnapshotQuality, Timeframe
from grow.options import IndexOptionsEngine
from grow.options.fixture import FixtureOptionChain
from grow.options.models import DecisionStatus
from grow.research.audit import AuditLog
from grow.research.ceo_agent import CEOAgent
from grow.research.models import CEOVerdict, Recommendation, Stance
from grow.research.orchestrator import ResearchOrchestrator
from grow.research.packet import build_packet
from grow.research.validate import DecisionValidator
from grow.strategies.signal import StrategySignal
from grow.types import SessionState, Symbol
from tests.test_options_engine import _signal, _snapshot


def _packet(direction: str = "BULLISH", *, ticker: str = "NIFTY", spot: float = 25000.0):
    config = load_config()
    as_of = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
    snap = _snapshot(ticker, as_of, spot)
    chain = FixtureOptionChain().snapshot(ticker, as_of, spot=spot)
    engine = IndexOptionsEngine(config)
    options = engine.evaluate(_signal(ticker, direction, as_of), snap, chain)
    view = research_view(snap)
    packet = build_packet(
        view=view,
        signal=_signal(ticker, direction, as_of),
        options=options,
        configuration_version=config.version,
    )
    return packet, options, config


class ResearchCEOTests(unittest.TestCase):
    def test_bullish_approves_ce_only(self) -> None:
        packet, options, config = _packet("BULLISH")
        self.assertEqual(options.status, DecisionStatus.CANDIDATE)
        decision = ResearchOrchestrator(config).run(packet)
        self.assertEqual(decision.decision, CEOVerdict.TRADE_APPROVE)
        self.assertEqual(decision.selected_candidate_id, packet.option_candidate_id)
        self.assertEqual(decision.direction, "BULLISH")
        self.assertEqual((packet.option_candidate or {}).get("option_type"), "CE")
        self.assertFalse(decision.to_dict()["executed"])

    def test_bearish_approves_pe_only(self) -> None:
        packet, options, config = _packet("BEARISH")
        decision = ResearchOrchestrator(config).run(packet)
        self.assertEqual(decision.decision, CEOVerdict.TRADE_APPROVE)
        self.assertEqual((packet.option_candidate or {}).get("option_type"), "PE")
        self.assertEqual(decision.direction, "BEARISH")

    def test_options_no_trade_propagates(self) -> None:
        packet, options, config = _packet("BULLISH")
        packet = replace(packet, options_status="NO_TRADE", option_candidate=None, option_candidate_id=None)
        decision = ResearchOrchestrator(config).run(packet)
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
        self.assertIn("OPTIONS_NO_TRADE", decision.rejection_reasons)

    def test_bull_bear_disagreement_preserved(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)
        decision = orch.run(packet)
        self.assertEqual(decision.decision, CEOVerdict.TRADE_APPROVE)
        self.assertTrue(decision.conflicting_report_ids)

    def test_quant_oppose_no_trade(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)
        reports = tuple(agent.research(packet) for agent in orch.agents)
        quant = next(r for r in reports if r.agent_id == "quant")
        quant = replace(quant, recommendation=Recommendation.OPPOSE)
        reports = tuple(quant if r.agent_id == "quant" else r for r in reports)
        raw = orch.ceo.synthesize(packet, reports)
        decision = orch.validator.validate(packet, raw, reports)
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
        self.assertIn("QUANT_OPPOSE", decision.rejection_reasons)

    def test_risk_oppose_no_trade(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)
        reports = tuple(agent.research(packet) for agent in orch.agents)
        risk = next(r for r in reports if r.agent_id == "risk_context")
        risk = replace(risk, recommendation=Recommendation.OPPOSE)
        reports = tuple(risk if r.agent_id == "risk_context" else r for r in reports)
        raw = orch.ceo.synthesize(packet, reports)
        decision = orch.validator.validate(packet, raw, reports)
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
        self.assertIn("RISK_CONTEXT_OPPOSE", decision.rejection_reasons)

    def test_insufficient_data_no_trade(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)
        reports = tuple(agent.research(packet) for agent in orch.agents)
        bull = next(r for r in reports if r.agent_id == "bull")
        bull = replace(bull, stance=Stance.INSUFFICIENT_DATA)
        reports = tuple(bull if r.agent_id == "bull" else r for r in reports)
        raw = orch.ceo.synthesize(packet, reports)
        decision = orch.validator.validate(packet, raw, reports)
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
        self.assertTrue(any("INSUFFICIENT_DATA" in r for r in decision.rejection_reasons))

    def test_unknown_candidate_rejected(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)
        raw = orch.ceo.synthesize(packet, tuple(a.research(packet) for a in orch.agents))
        raw = replace(raw, selected_candidate_id="not-a-real-id")
        decision = orch.validator.validate(packet, raw, tuple(a.research(packet) for a in orch.agents))
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
        self.assertIn("UNKNOWN_CANDIDATE", decision.rejection_reasons)

    def test_bullish_pe_rejected(self) -> None:
        packet, _, config = _packet("BULLISH")
        cand = dict(packet.option_candidate or {})
        cand["option_type"] = "PE"
        packet = replace(packet, option_candidate=cand)
        orch = ResearchOrchestrator(config)
        reports = tuple(a.research(packet) for a in orch.agents)
        raw = orch.ceo.synthesize(packet, reports)
        decision = orch.validator.validate(packet, raw, reports)
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)

    def test_bearish_ce_rejected(self) -> None:
        packet, _, config = _packet("BEARISH")
        cand = dict(packet.option_candidate or {})
        cand["option_type"] = "CE"
        packet = replace(packet, option_candidate=cand)
        orch = ResearchOrchestrator(config)
        reports = tuple(a.research(packet) for a in orch.agents)
        raw = orch.ceo.synthesize(packet, reports)
        decision = orch.validator.validate(packet, raw, reports)
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)

    def test_neutral_with_candidate_rejected(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)
        reports = tuple(a.research(packet) for a in orch.agents)
        raw = orch.ceo.synthesize(packet, reports)
        raw = replace(raw, direction="NEUTRAL")
        decision = orch.validator.validate(packet, raw, reports)
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
        self.assertIn("NEUTRAL_WITH_CANDIDATE", decision.rejection_reasons)

    def test_sell_short_rejected(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)
        reports = tuple(a.research(packet) for a in orch.agents)
        raw = orch.ceo.synthesize(packet, reports)
        for side in ("SELL", "SHORT"):
            decision = orch.validator.validate(packet, replace(raw, direction=side), reports)
            self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
            self.assertTrue(any("EXECUTION_LANGUAGE" in r for r in decision.rejection_reasons))

    def test_prohibited_execution_fields(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)
        reports = tuple(a.research(packet) for a in orch.agents)
        raw = orch.ceo.synthesize(packet, reports)
        decision = orch.validator.validate(packet, raw, reports, raw={"quantity": 50, "broker": "kite"})
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
        self.assertTrue(any("PROHIBITED_FIELD" in r for r in decision.rejection_reasons))

    def test_candidate_mutation_rejected(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)
        reports = tuple(a.research(packet) for a in orch.agents)
        raw_dec = orch.ceo.synthesize(packet, reports)
        cand = packet.option_candidate or {}
        decision = orch.validator.validate(
            packet,
            raw_dec,
            reports,
            raw={"strike": (cand.get("strike") or 0) + 50, "expiry": "2099-01-01", "premium_reference": 0.01},
        )
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
        self.assertTrue(any("CANDIDATE_MUTATION" in r for r in decision.rejection_reasons))

    def test_timeout_no_trade(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)

        class Boom:
            agent_id = "bull"
            agent_version = "v1"
            prompt_version = "v1"

            def research(self, packet):
                raise TimeoutError("llm")

        orch.agents = (Boom(),) + orch.agents[1:]  # type: ignore[assignment]
        decision = orch.run(packet)
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
        self.assertTrue(any("PROVIDER_TIMEOUT" in r for r in decision.rejection_reasons))

    def test_provider_failure_no_trade(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)

        class Boom:
            agent_id = "quant"
            agent_version = "v1"
            prompt_version = "v1"

            def research(self, packet):
                raise RuntimeError("down")

        orch.agents = (orch.agents[0], orch.agents[1], Boom(), orch.agents[3])  # type: ignore[assignment]
        decision = orch.run(packet)
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
        self.assertTrue(any("PROVIDER_FAILURE" in r for r in decision.rejection_reasons))

    def test_determinism_and_audit(self) -> None:
        packet, _, config = _packet("BULLISH")
        log = AuditLog()
        a = ResearchOrchestrator(config, audit=log).run(packet)
        b = ResearchOrchestrator(config, audit=log).run(packet)
        self.assertEqual(a.to_dict(), b.to_dict())
        self.assertEqual(len(log.records()), 2)
        self.assertEqual(log.records()[0].packet_id, packet.packet_id)
        self.assertFalse(hasattr(log, "delete"))
        self.assertFalse(hasattr(log, "rewrite"))

    def test_no_execution_surface(self) -> None:
        src = inspect.getsource(ResearchOrchestrator) + inspect.getsource(CEOAgent) + inspect.getsource(DecisionValidator)
        self.assertNotIn("def place_order", src)
        self.assertNotIn("def execute", src)
        self.assertNotIn("import grow.paper", src)
        self.assertNotIn("import grow.risk", src)
        self.assertNotIn("kite", src.lower())

    def test_packet_schema_and_no_ohlcv(self) -> None:
        packet, _, _ = _packet("BULLISH")
        payload = packet.to_dict()
        self.assertEqual(packet.packet_schema_version, "research.packet.v1")
        blob = str(payload).lower()
        self.assertNotIn("'volume'", str(payload.get("option_candidate")))
        self.assertIn("option_volume", payload["option_candidate"])
        self.assertNotIn("bars", payload["market_research_view"])

    def test_stale_view_risk_opposes(self) -> None:
        packet, _, config = _packet("BULLISH")
        ctx = dict(packet.session_context)
        ctx["quality_stale"] = True
        packet = replace(packet, session_context=ctx)
        orch = ResearchOrchestrator(config)
        decision = orch.run(packet)
        self.assertEqual(decision.decision, CEOVerdict.NO_TRADE)
        self.assertTrue(any("RISK_CONTEXT_OPPOSE" in r or "INSUFFICIENT" in r for r in decision.rejection_reasons))

    def test_ceo_cannot_invent_candidate(self) -> None:
        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)
        reports = tuple(a.research(packet) for a in orch.agents)
        raw = orch.ceo.synthesize(packet, reports)
        self.assertEqual(raw.selected_candidate_id, packet.option_candidate_id)
        self.assertNotIn("strike", raw.to_dict() or {})
        self.assertIsNone(raw.to_dict().get("quantity"))


if __name__ == "__main__":
    unittest.main()
