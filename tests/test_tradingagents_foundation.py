from __future__ import annotations

import unittest
from datetime import datetime

from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.market.research import MarketResearch
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.research_graph import ResearchGraph


class TradingAgentsFoundationTests(unittest.TestCase):
    def test_default_config_is_mock_and_stub(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["llm_provider"], "mock")
        self.assertEqual(DEFAULT_CONFIG["data_vendors"]["core_stock_apis"], "stub")
        self.assertEqual(DEFAULT_CONFIG["timezone"], "Asia/Kolkata")

    def test_graph_returns_bundle_not_orders(self) -> None:
        config = load_config()
        clock = FrozenClock(datetime(2026, 9, 21, 11, 0, tzinfo=IST))
        brief = MarketResearch(config, clock=clock).research("INFY")
        bundle = ResearchGraph(config).propagate("infy", "2026-09-21", brief=brief)
        self.assertEqual(bundle.ticker, "INFY")
        roles = {n.role for n in bundle.notes}
        self.assertGreaterEqual(
            roles,
            {"fundamentals", "sentiment", "news", "technical", "bull_researcher", "bear_researcher", "trader"},
        )
        self.assertIn("paper", bundle.trader_plan.lower())
        dumped = bundle.to_dict()
        self.assertNotIn("order", dumped)
        self.assertNotIn("broker", dumped)
