from __future__ import annotations

import inspect
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta

from grow.backtest.calendar import ExplicitSessionCalendar, WeekdayFixtureCalendar, weekday_sessions
from grow.backtest.costs import CostModel, SlippageModel, contract_pnl
from grow.backtest.pipeline import _path_exit, resolve_ceo
from grow.backtest.runner import BacktestRunner, build_manifest
from grow.backtest.simulate import ExecutionSimulator
from grow.backtest.walkforward import WalkForwardRunner
from grow.clock import IST
from grow.config import load_config
from grow.data.schema import Bar, Timeframe
from grow.errors import GrowConfigError
from grow.options.models import OptionCandidate, ScoreBreakdown
from grow.strategies.signal import StrategySignal
from grow.types import Symbol
from tests.helpers import make_runtime


def _candidate(*, ask: float | None, bid: float | None = 10.0) -> OptionCandidate:
    as_of = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
    return OptionCandidate(
        candidate_id="cand-1",
        underlying="NIFTY",
        direction="BULLISH",
        option_type="CE",
        intent="BUY",
        expiry=date(2026, 9, 22),
        strike=25000.0,
        contract_symbol="NIFTY-CE",
        spot_price=25000.0,
        premium_reference=ask or 10.0,
        bid=bid,
        ask=ask,
        spread=None if ask is None or bid is None else ask - bid,
        spread_pct=None,
        volume=1000,
        open_interest=5000,
        implied_volatility=None,
        delta=None,
        gamma=None,
        theta=None,
        vega=None,
        intrinsic_value=0.0,
        extrinsic_value=ask or 10.0,
        moneyness="ATM",
        score=ScoreBreakdown("v1", 0.5, {}, {}, "x"),
        as_of=as_of,
        underlying_snapshot_id="u",
        option_chain_snapshot_id="c",
        strategy_signal_id="s",
        strategy_version="v1",
        selection_version="v1",
        reasons=(),
    )


class BacktestTests(unittest.TestCase):
    def test_ask_plus_slippage_and_unknown_quote(self) -> None:
        sim = ExecutionSimulator(SlippageModel(10), quantity=1, strict=True)
        as_of = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        filled = sim.enter(_candidate(ask=100.0, bid=99.0), as_of=as_of)
        self.assertNotIsInstance(filled, str)
        self.assertGreater(filled.price, 100.0)
        self.assertEqual(sim.enter(_candidate(ask=None), as_of=as_of), "UNKNOWN_QUOTE")
        self.assertEqual(sim.enter(_candidate(ask=10.0, bid=12.0), as_of=as_of), "CROSSED_QUOTE")

    def test_stop_first_ambiguous(self) -> None:
        as_of = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        signal = StrategySignal(
            symbol=Symbol("NIFTY"),
            strategy="ema_trend",
            direction="BULLISH",
            entry=25000.0,
            stop=24900.0,
            target=25100.0,
            confidence=0.6,
            timeframe=Timeframe.M15,
            reason="t",
            as_of=as_of,
            snapshot_id="s",
            signal_id="sid",
        )
        bar = Bar(
            symbol=Symbol("NIFTY"),
            timeframe=Timeframe.M15,
            start=as_of,
            end=as_of + timedelta(minutes=15),
            open=25000.0,
            high=25200.0,
            low=24800.0,
            close=25000.0,
            volume=1,
        )
        when, reason = _path_exit(signal, (bar,), as_of)
        self.assertEqual(reason, "STOP_FIRST_AMBIGUOUS")
        self.assertEqual(when, bar.end)

    def test_one_session_replay_isolated_from_paper(self) -> None:
        config = load_config()
        paper = make_runtime(config).ledger
        before = list(paper.book.fills)
        result = BacktestRunner(config).run(
            start=date(2026, 9, 21),
            end=date(2026, 9, 21),
            underlyings=("NIFTY",),
        )
        self.assertTrue(result.coverage["complete"])
        self.assertEqual(result.manifest.fill_model, "ask_plus_slippage")
        self.assertFalse(result.manifest.to_dict()["live"])
        self.assertEqual(result.metrics["label"], "HISTORICAL RESEARCH / NOT LIVE")
        self.assertEqual(len(result.ledger.decisions), 1)
        self.assertEqual(list(paper.book.fills), before)
        if result.ledger.primary_trades:
            trade = result.ledger.primary_trades[0]
            self.assertNotEqual(trade.gross_pnl, trade.net_pnl)
            self.assertGreater(trade.total_cost, 0)
            self.assertEqual(trade.option_type in {"CE", "PE"}, True)
            self.assertEqual(trade.direction in {"BULLISH", "BEARISH"}, True)

    def test_determinism(self) -> None:
        config = load_config()
        a = BacktestRunner(config).run(start=date(2026, 9, 21), end=date(2026, 9, 21), underlyings=("NIFTY",))
        b = BacktestRunner(config).run(start=date(2026, 9, 21), end=date(2026, 9, 21), underlyings=("NIFTY",))
        self.assertEqual(a.manifest.fingerprint, b.manifest.fingerprint)
        self.assertEqual(a.to_dict()["metrics"], b.to_dict()["metrics"])
        self.assertEqual([t.to_dict() for t in a.ledger.trades], [t.to_dict() for t in b.ledger.trades])

    def test_always_no_trade_ablation(self) -> None:
        result = BacktestRunner().run(
            start=date(2026, 9, 21),
            end=date(2026, 9, 21),
            underlyings=("NIFTY",),
            ablation="always_no_trade",
        )
        self.assertEqual(result.metrics["trade_count"], 0)
        self.assertEqual(result.ledger.decisions[0].reason, "ABLATION_ALWAYS_NO_TRADE")

    def test_walk_forward_rejects_test_tuning(self) -> None:
        with self.assertRaises(GrowConfigError) as ctx:
            WalkForwardRunner().run(start=date(2026, 9, 1), end=date(2026, 9, 18), calibrate_on_test=True)
        self.assertIn("TEST_WINDOW_TUNING", str(ctx.exception))

    def test_walk_forward_frozen_windows(self) -> None:
        result = WalkForwardRunner().run(start=date(2026, 9, 1), end=date(2026, 9, 18))
        self.assertGreaterEqual(len(result.windows), 1)
        window = result.windows[0]
        self.assertLess(window.train[1], window.validate[0])
        self.assertLess(window.validate[1], window.test[0])
        self.assertIn("always_no_trade", result.ablations)
        self.assertEqual(result.ablations["always_no_trade"], 0.0)
        self.assertFalse(result.plan["calibrate_on_test"])
        self.assertEqual(result.plan["calibration_mode"], "NONE")
        self.assertEqual(result.plan["trainable_parameters"], ())
        standalone = BacktestRunner().run(start=window.test[0], end=window.test[1], ablation="full")
        self.assertEqual(result.test_results[0].metrics["net_pnl"], standalone.metrics["net_pnl"])
        self.assertEqual(result.test_results[0].metrics["trade_count"], standalone.metrics["trade_count"])

    def test_manifest_requires_versions(self) -> None:
        config = load_config()
        man = build_manifest(config, start=date(2026, 9, 21), end=date(2026, 9, 21), ablation="full")
        self.assertTrue(man.cost_model_version)
        self.assertTrue(man.fingerprint)
        self.assertIsNone(man.random_seed)

    def test_pnl_is_price_delta_times_lot_size_times_lots(self) -> None:
        self.assertEqual(contract_pnl(entry=100.0, exit=110.0, lots=2, lot_size=75), 1500.0)
        self.assertEqual(contract_pnl(entry=50.0, exit=40.0, lots=1, lot_size=15), -150.0)
        cheap = CostModel().round_trip(entry=100, exit=110, quantity=1, lot_size=1)
        dear = CostModel().round_trip(entry=100, exit=110, quantity=1, lot_size=75)
        self.assertGreater(dear, cheap)

    def test_recorded_ceo_drives_decision(self) -> None:
        from grow.research.orchestrator import ResearchOrchestrator
        from grow.research.validate import no_trade
        from tests.test_research_ceo import _packet

        packet, _, config = _packet("BULLISH")
        orch = ResearchOrchestrator(config)
        live = orch.run(packet)
        forced = no_trade(packet, reasons=("RECORDED_FORCE_NO_TRADE",), validation_ok=True)
        replayed = resolve_ceo(packet, orch, {packet.packet_id: forced})
        self.assertEqual(replayed.decision.value, "NO_TRADE")
        self.assertIn("RECORDED_FORCE_NO_TRADE", replayed.rejection_reasons)
        missing = resolve_ceo(packet, orch, {})
        self.assertIn("MISSING_RECORDED_AI", missing.rejection_reasons)
        if live.decision.value == "TRADE_APPROVE":
            self.assertNotEqual(replayed.decision, live.decision)

    def test_explicit_calendar_not_weekdays(self) -> None:
        fixture = WeekdayFixtureCalendar().sessions(date(2026, 9, 21), date(2026, 9, 22))
        self.assertEqual(fixture, (date(2026, 9, 21), date(2026, 9, 22)))
        holiday = ExplicitSessionCalendar((date(2026, 9, 21),))
        self.assertEqual(holiday.version, "nse.session.explicit.v1")
        self.assertEqual(holiday.sessions(date(2026, 9, 21), date(2026, 9, 22)), (date(2026, 9, 21),))
        runner = BacktestRunner(calendar=holiday)
        result = runner.run(start=date(2026, 9, 21), end=date(2026, 9, 22), underlyings=("NIFTY",))
        self.assertEqual(result.coverage["sessions"], 1)
        self.assertEqual(result.manifest.calendar_version, "nse.session.explicit.v1")

    def test_no_execution_surface(self) -> None:
        from grow.backtest import runner, pipeline, ledger, simulate

        src = inspect.getsource(runner) + inspect.getsource(pipeline) + inspect.getsource(ledger) + inspect.getsource(simulate)
        self.assertNotIn("import grow.paper", src)
        self.assertNotIn("import grow.risk", src)
        self.assertNotIn("def place_order", src)
        self.assertNotIn("kite", src.lower())

    def test_weekday_calendar(self) -> None:
        days = weekday_sessions(date(2026, 9, 19), date(2026, 9, 21))
        self.assertEqual(days, (date(2026, 9, 21),))

    def test_recorded_ai_missing_no_trade(self) -> None:
        from grow.backtest.pipeline import DecisionPipeline
        from grow.backtest.ledger import BacktestLedger
        from grow.data.factory import open_data_hub
        from grow.options.engine import IndexOptionsEngine
        from grow.options.fixture import open_option_source
        from grow.research.orchestrator import ResearchOrchestrator
        from grow.strategies.engine import StrategyEngine

        config = load_config()
        as_of = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        ledger = BacktestLedger()
        pipe = DecisionPipeline(
            hub=open_data_hub(config),
            strategies=StrategyEngine(config),
            options=IndexOptionsEngine(config),
            chains=open_option_source(),
            research=ResearchOrchestrator(config),
            simulator=ExecutionSimulator(SlippageModel(10)),
            costs=CostModel(),
            ledger=ledger,
            run_id="t",
            ablation="full",
            config_version=config.version,
            recorded={},
        )
        pipe.evaluate("NIFTY", as_of)
        self.assertTrue(any(d.reason in {"MISSING_RECORDED_AI", "NO_SIGNAL", "OPTIONS_NO_TRADE"} or d.status == "NO_TRADE" for d in ledger.decisions))
        if any(d.reason == "MISSING_RECORDED_AI" for d in ledger.decisions):
            self.assertEqual(ledger.trades, [])

    def test_future_chain_rejected(self) -> None:
        from grow.backtest.pipeline import DecisionPipeline
        from grow.backtest.ledger import BacktestLedger
        from grow.data.factory import open_data_hub
        from grow.options.engine import IndexOptionsEngine
        from grow.options.fixture import FixtureOptionChain
        from grow.research.orchestrator import ResearchOrchestrator
        from grow.strategies.engine import StrategyEngine

        class FutureChain(FixtureOptionChain):
            def snapshot(self, underlying, as_of, *, spot):
                snap = super().snapshot(underlying, as_of, spot=spot)
                return replace(snap, as_of=as_of + timedelta(hours=1))

        config = load_config()
        as_of = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
        ledger = BacktestLedger()
        pipe = DecisionPipeline(
            hub=open_data_hub(config),
            strategies=StrategyEngine(config),
            options=IndexOptionsEngine(config),
            chains=FutureChain(),
            research=ResearchOrchestrator(config),
            simulator=ExecutionSimulator(SlippageModel(10)),
            costs=CostModel(),
            ledger=ledger,
            run_id="t",
            ablation="full",
            config_version=config.version,
        )
        pipe.evaluate("NIFTY", as_of)
        reasons = {d.reason for d in ledger.decisions}
        self.assertTrue("LOOKAHEAD_CHAIN" in reasons or "NO_SIGNAL" in reasons)


if __name__ == "__main__":
    unittest.main()
