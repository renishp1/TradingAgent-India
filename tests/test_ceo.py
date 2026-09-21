from __future__ import annotations

import inspect
import unittest
from datetime import datetime

from grow.ceo.ceo import CEO
from grow.clock import FrozenClock, IST
from grow.config import load_config
from grow.cycle import GrowRuntime
from grow.market.research import MarketResearch
from grow.model_gateway.gateway import ModelGateway
from grow.types import Venue


class CEOTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(datetime(2026, 9, 21, 11, 0, tzinfo=IST))
        self.config = load_config()
        self.ceo = CEO(self.config, ModelGateway(self.config), clock=self.clock)
        self.brief = MarketResearch(self.config, clock=self.clock).research("TCS")

    def test_propose_is_paper_only(self) -> None:
        proposal = self.ceo.propose(self.brief)
        self.assertIs(proposal.venue, Venue.PAPER)
        self.assertGreater(proposal.quantity, 0)
        self.assertIsNotNone(proposal.stop_loss)

    def test_ceo_has_no_execution_methods(self) -> None:
        names = {name for name, _ in inspect.getmembers(CEO, predicate=inspect.isfunction)}
        forbidden = {"submit", "place_order", "place_live_order", "fill", "connect_broker"}
        self.assertTrue(names.isdisjoint(forbidden), names & forbidden)

    def test_cycle_does_not_fill_when_guard_rejects(self) -> None:
        night = FrozenClock(datetime(2026, 9, 21, 22, 0, tzinfo=IST))
        runtime = GrowRuntime(self.config, clock=night)
        report = runtime.run("INFY")
        self.assertFalse(report.verdict.approved)
        self.assertIsNone(report.fill)
        self.assertEqual(report.book["fills"], [])
