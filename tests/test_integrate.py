from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import date, datetime

from grow.backtest.costs import CostModel, SlippageModel, contract_pnl
from grow.backtest.ledger import BacktestLedger
from grow.backtest.pipeline import DecisionPipeline
from grow.backtest.simulate import ExecutionSimulator
from grow.clock import IST
from grow.config import load_config
from grow.data.factory import open_data_hub
from grow.data.schema import Timeframe
from grow.director.director import FixtureDirector
from grow.errors import GrowConfigError
from grow.execution.lock import LIVE_TRADING_COMPILED
from grow.history.bridge import HistoricalMarketSource, HistoricalOptionSource
from grow.history.eval import ProviderEvaluationRunner
from grow.history.expiry import select_nearest_weekly_expiry, universe_at
from grow.history.integrate.contract import ADAPTER_VERSION, RECORDED_PROVIDER_ID, VENDOR_SCHEMA, AcquireScope
from grow.history.integrate.normalize import canonical_contract_id
from grow.history.integrate.pipeline import ingest
from grow.history.integrate.recorded import open_provider
from grow.history.integrate.replay import (
    bind_director_catalog,
    historical_config,
    open_qualified_runner,
    require_qualified_real,
)
from grow.history.integrate.sample import (
    END,
    START,
    WEEKLY_NEXT,
    WEEKLY_SAME,
    recorded_payload,
    recorded_scope,
)
from grow.history.integrate.secrets import provider_secret, strip_secret_headers, strip_secrets
from grow.history.models import (
    ADAPTER_TESTING,
    APPROVED_FOR_2E,
    OPTION_SNAPSHOT_GAPS,
    QUALIFIED,
    QUALIFIED_FOR_ADAPTER_TESTING,
)
from grow.history.universe import discover_underlyings
from grow.options.engine import IndexOptionsEngine
from grow.options.models import DecisionStatus
from grow.research.orchestrator import ResearchOrchestrator
from grow.strategies.models import IndicatorSnapshot, MarketRegime, RegimeSnapshot, StrategyResult
from grow.strategies.signal import StrategySignal


def _as_of(day: date = START, hh: int = 11, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


def _ingest(**flags):
    return ingest(recorded_scope(), payload=recorded_payload(**flags))


class ProviderAdapterTests(unittest.TestCase):
    def test_open_provider_is_explicit_and_rejects_fixture_live_and_unknown(self) -> None:
        self.assertEqual(open_provider(RECORDED_PROVIDER_ID).identity, RECORDED_PROVIDER_ID)
        self.assertEqual(open_provider(RECORDED_PROVIDER_ID).adapter_version, ADAPTER_VERSION)
        for bad, code in (
            ("fixture", "FIXTURE_FALLBACK_FORBIDDEN"),
            ("grow.history.sample.v1", "FIXTURE_FALLBACK_FORBIDDEN"),
            ("grow.history.eval.sample.v1", "FIXTURE_FALLBACK_FORBIDDEN"),
            ("live.nse", "PROVIDER_NOT_APPROVED"),
            ("truedata", "PROVIDER_NOT_APPROVED"),
            ("nse.official", "PROVIDER_NOT_APPROVED"),
        ):
            with self.assertRaises(GrowConfigError) as ctx:
                open_provider(bad)
            self.assertIn(code, str(ctx.exception))

    def test_acquire_rejects_fixture_payload_and_missing_body(self) -> None:
        provider = open_provider(RECORDED_PROVIDER_ID)
        with self.assertRaises(GrowConfigError) as ctx:
            provider.acquire(recorded_scope())
        self.assertIn("RECORDED_PAYLOAD_REQUIRED", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            provider.acquire(recorded_scope(), payload={"provider": "fixture", "vendor_schema": VENDOR_SCHEMA})
        self.assertIn("FIXTURE_FALLBACK_FORBIDDEN", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            provider.acquire(recorded_scope(), payload={"provider": RECORDED_PROVIDER_ID, "vendor_schema": "other"})
        self.assertIn("UNKNOWN_VENDOR_SCHEMA", str(ctx.exception))

    def test_pagination_flattens_and_retries_are_observable(self) -> None:
        base = recorded_payload()
        paged = {
            **{k: v for k, v in base.items() if k not in {"calendar", "spot_bars", "contract_master", "option_quotes"}},
            "pages": [
                {"calendar": base["calendar"], "spot_bars": base["spot_bars"]},
                {"contract_master": base["contract_master"], "option_quotes": base["option_quotes"]},
            ],
            "transient_failures": 2,
            "rate_limit_remaining": 7,
        }
        artifact = open_provider(RECORDED_PROVIDER_ID).acquire(recorded_scope(), payload=paged, max_retries=2)
        self.assertEqual(artifact.retry_count, 2)
        self.assertEqual(artifact.rate_limit_remaining, 7)
        self.assertEqual(len(artifact.payload["calendar"]), len(base["calendar"]))
        self.assertEqual(len(artifact.payload["contract_master"]), len(base["contract_master"]))
        exhausted = dict(paged)
        exhausted["transient_failures"] = 4
        with self.assertRaises(GrowConfigError) as ctx:
            open_provider(RECORDED_PROVIDER_ID).acquire(recorded_scope(), payload=exhausted, max_retries=2)
        self.assertIn("PROVIDER_TRANSIENT_EXHAUSTED", str(ctx.exception))

    def test_artifact_fingerprint_is_reproducible(self) -> None:
        payload = recorded_payload()
        a = open_provider(RECORDED_PROVIDER_ID).acquire(recorded_scope(), payload=payload)
        b = open_provider(RECORDED_PROVIDER_ID).acquire(recorded_scope(), payload=payload)
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(a.vendor_schema, VENDOR_SCHEMA)
        self.assertFalse(a.to_dict()["live"])


class SecretTests(unittest.TestCase):
    def test_secret_comes_from_env_and_never_from_empty(self) -> None:
        self.assertEqual(provider_secret("API_KEY", {"GROW_HISTORICAL_API_KEY": "k-test"}), "k-test")
        with self.assertRaises(GrowConfigError) as ctx:
            provider_secret("API_KEY", {})
        self.assertIn("PROVIDER_SECRET_MISSING:GROW_HISTORICAL_API_KEY", str(ctx.exception))

    def test_authorization_headers_are_stripped_from_artifacts(self) -> None:
        payload = recorded_payload()
        payload["response_headers"] = {
            "Authorization": "Bearer super-secret",
            "x-api-key": "abc",
            "Content-Type": "application/json",
        }
        payload["api_key"] = "should-not-store"
        artifact = open_provider(RECORDED_PROVIDER_ID).acquire(recorded_scope(), payload=payload)
        dumped = str(artifact.to_dict()) + str(dict(artifact.payload)) + str(dict(artifact.headers))
        self.assertNotIn("super-secret", dumped)
        self.assertNotIn("should-not-store", dumped)
        self.assertNotIn("abc", dumped)
        self.assertEqual(artifact.headers.get("Content-Type"), "application/json")
        self.assertNotIn("Authorization", artifact.headers)
        self.assertEqual(strip_secret_headers({"Authorization": "x", "Accept": "a"})["Accept"], "a")
        self.assertNotIn("api_key", strip_secrets({"api_key": "x", "provider": RECORDED_PROVIDER_ID}))


class NormalizeTests(unittest.TestCase):
    def test_canonical_identity_differs_from_tradingsymbol_and_keeps_lot_size(self) -> None:
        result = _ingest()
        nifty = [c for c in result.store.all_contracts() if c.underlying == "NIFTY" and c.option_type == "CE"]
        self.assertTrue(nifty)
        row = nifty[0]
        self.assertEqual(
            row.contract_id,
            canonical_contract_id(row.underlying, row.expiry.isoformat(), row.strike, row.option_type),
        )
        self.assertNotEqual(row.contract_id, row.provider_contract_id)
        self.assertEqual(row.lot_size, 75)
        bank = next(c for c in result.store.all_contracts() if c.underlying == "BANKNIFTY")
        self.assertEqual(bank.lot_size, 15)
        mid = next(c for c in result.store.all_contracts() if c.underlying == "MIDCPNIFTY")
        self.assertEqual(mid.lot_size, 75)
        self.assertEqual(mid.expiry_class, "MONTHLY")

    def test_naive_timestamps_fail_at_canonical_boundary(self) -> None:
        with self.assertRaises(GrowConfigError) as ctx:
            _ingest(naive=True)
        self.assertIn("NAIVE_TIMESTAMP", str(ctx.exception))

    def test_identity_ambiguity_missing_lot_and_futures_fail_closed(self) -> None:
        payload = recorded_payload()
        original = payload["contract_master"][0]
        clash = dict(original)
        clash["tradingsymbol"] = original["tradingsymbol"] + "X"
        payload["contract_master"] = [original, clash, *payload["contract_master"][1:]]
        with self.assertRaises(GrowConfigError) as ctx:
            ingest(recorded_scope(), payload=payload)
        self.assertIn("IDENTITY_AMBIGUITY", str(ctx.exception))

        missing = recorded_payload()
        missing["contract_master"][0] = {**missing["contract_master"][0], "lot_size": None}
        with self.assertRaises(GrowConfigError) as ctx:
            ingest(recorded_scope(), payload=missing)
        self.assertIn("MISSING_LOT_SIZE", str(ctx.exception))

        fut = recorded_payload()
        fut["contract_master"].append({**fut["contract_master"][0], "tradingsymbol": "NIFTY26SEPFUT", "instrument_type": "FUT"})
        with self.assertRaises(GrowConfigError) as ctx:
            ingest(recorded_scope(), payload=fut)
        self.assertIn("UNSUPPORTED_INSTRUMENT_TYPE", str(ctx.exception))

        stock = recorded_payload()
        stock["spot_bars"].append({**stock["spot_bars"][0], "underlying": "RELIANCE"})
        with self.assertRaises(GrowConfigError) as ctx:
            ingest(recorded_scope(), payload=stock)
        self.assertIn("UNSUPPORTED_UNDERLYING", str(ctx.exception))

    def test_iv_and_greeks_stay_missing(self) -> None:
        result = _ingest()
        quotes = result.store.all_quotes()
        self.assertTrue(quotes)
        self.assertTrue(all(q.implied_volatility is None for q in quotes))
        self.assertTrue(all(q.delta is None and q.gamma is None for q in quotes))
        self.assertFalse(result.store.meta.iv_available)
        self.assertFalse(result.store.meta.greeks_available)


class QualificationTests(unittest.TestCase):
    def test_ingest_qualifies_recorded_sample_with_pit_lifecycle(self) -> None:
        result = _ingest()
        self.assertEqual(
            result.lifecycle,
            (
                "RAW_ACQUIRED",
                "NORMALIZED",
                "PIT_VALIDATED",
                "QUALIFICATION_REVIEW",
                "QUALIFIED_FOR_ADAPTER_TESTING",
            ),
        )
        self.assertEqual(result.qualification.qualification_status, QUALIFIED_FOR_ADAPTER_TESTING)
        self.assertNotEqual(result.qualification.qualification_status, APPROVED_FOR_2E)
        self.assertFalse(result.qualification.approved_for_2e)
        self.assertEqual(result.store.meta.usage_scope, ADAPTER_TESTING)
        self.assertEqual(result.store.meta.license_status, "NOT_APPROVED")
        self.assertEqual(result.store.meta.fingerprint, result.qualification.fingerprint)
        self.assertNotEqual(result.store.meta.fingerprint, "pending")
        self.assertFalse(result.store.meta.is_fixture)
        self.assertEqual(result.provider_id, RECORDED_PROVIDER_ID)
        self.assertEqual(result.adapter_version, ADAPTER_VERSION)
        self.assertTrue(result.policy_fingerprint)
        again = _ingest()
        self.assertEqual(again.store.meta.fingerprint, result.store.meta.fingerprint)

    def test_recorded_sample_cannot_pass_require_qualified_real(self) -> None:
        result = _ingest()
        self.assertEqual(result.qualification.qualification_status, QUALIFIED_FOR_ADAPTER_TESTING)
        self.assertFalse(result.qualification.approved_for_2e)
        with self.assertRaises(GrowConfigError) as ctx:
            require_qualified_real(result.store, result.qualification)
        self.assertIn("NOT_APPROVED_FOR_2E", str(ctx.exception))

    def test_pit_failure_does_not_emit_pit_validated(self) -> None:
        from unittest.mock import patch

        from grow.history.eval import CheckResult

        with patch("grow.history.eval._pit", return_value=CheckResult("PIT", "FAIL", "injected", True)):
            result = _ingest()
        self.assertNotIn("PIT_VALIDATED", result.lifecycle)
        self.assertIn("PIT:FAIL", result.qualification.checks)
        self.assertEqual(result.lifecycle[-1], "REJECTED")
        self.assertNotEqual(result.qualification.qualification_status, APPROVED_FOR_2E)

    def test_incomplete_coverage_is_not_qualified_for_replay(self) -> None:
        result = _ingest(incomplete=True)
        self.assertEqual(result.lifecycle[-1], "REJECTED")
        self.assertNotIn(result.qualification.qualification_status, {QUALIFIED, APPROVED_FOR_2E})
        self.assertIn(OPTION_SNAPSHOT_GAPS, result.qualification.quality_warnings)
        with self.assertRaises(GrowConfigError) as ctx:
            require_qualified_real(result.store, result.qualification)
        self.assertIn("NOT_APPROVED_FOR_2E", str(ctx.exception))

    def test_future_contract_is_invisible_before_listed_from(self) -> None:
        result = _ingest(extra_future=True)
        store = result.store
        as_of = _as_of(END, 15, 15)
        contracts, quotes = store.snapshot_quotes("NIFTY", as_of)
        self.assertFalse(any(c.expiry == date(2026, 10, 6) for c in contracts))
        self.assertFalse(any("2026-10-06" in q.contract_id for q in quotes))
        later, later_q = store.snapshot_quotes("NIFTY", datetime(2026, 9, 21, 11, 0, tzinfo=IST))
        self.assertTrue(any(c.expiry == date(2026, 10, 6) for c in later))
        self.assertTrue(any(q.contract_id.endswith("25000-CE") for q in later_q))
        pit = [c for c in ProviderEvaluationRunner().evaluate(store).checks if c.name == "PIT"]
        self.assertEqual(pit[0].outcome, "PASS")

    def test_discovery_is_not_hardcoded_to_nifty_banknifty(self) -> None:
        result = _ingest()
        found = discover_underlyings(result.store, _as_of())
        by_id = {row.canonical_symbol: row for row in found}
        self.assertEqual(by_id["NIFTY"].status, "ELIGIBLE")
        self.assertEqual(by_id["BANKNIFTY"].status, "ELIGIBLE")
        self.assertEqual(by_id["MIDCPNIFTY"].status, "ELIGIBLE")
        self.assertEqual(by_id["MIDCPNIFTY"].expiry_policy_profile, "MONTHLY_ONLY")
        self.assertNotIn("RELIANCE", by_id)
        as_of = _as_of()
        visible = universe_at(result.store, "NIFTY", as_of)
        chosen = select_nearest_weekly_expiry("NIFTY", as_of, visible, allow_same_day=False)
        self.assertEqual(chosen, WEEKLY_SAME)
        same_day = universe_at(result.store, "NIFTY", _as_of(WEEKLY_SAME))
        self.assertEqual(select_nearest_weekly_expiry("NIFTY", _as_of(WEEKLY_SAME), same_day), WEEKLY_NEXT)


class ReplayE2ETests(unittest.TestCase):
    def test_2c_2d_2e_replay_uses_historical_lot_size_not_config_default(self) -> None:
        result = _ingest()
        base_config = load_config()
        config = historical_config(base_config)
        self.assertEqual(config.options.provider, "historical")
        self.assertFalse(config.options.allow_live_chain)
        self.assertEqual(config.backtest.provider, "fixture")
        self.assertEqual(config.backtest.lot_size, 1)
        store = result.store
        as_of = _as_of()
        hub = open_data_hub(config, source=HistoricalMarketSource(store, config))
        chains = HistoricalOptionSource(store)
        snap = hub.snapshot("NIFTY", as_of=as_of)
        chain = chains.snapshot("NIFTY", as_of, spot=snap.last_price)
        self.assertFalse(chain.is_fixture)
        self.assertNotIn("live", chain.source_id.lower())

        class StubStrategies:
            def evaluate(self, market):
                signal = StrategySignal(
                    symbol=market.symbol,
                    strategy="ema_trend",
                    direction="BULLISH",
                    entry=100.0,
                    stop=90.0,
                    target=120.0,
                    confidence=0.6,
                    timeframe=Timeframe.M15,
                    reason="stub",
                    as_of=market.as_of,
                    snapshot_id=market.snapshot_id,
                    signal_id="sig-2j",
                    strategy_version="v1",
                )
                return StrategyResult(
                    snapshot_id=market.snapshot_id,
                    as_of=market.as_of,
                    symbol=market.symbol,
                    regime=RegimeSnapshot(MarketRegime.BULL_TREND, "up", "up", "normal", "stub"),
                    signals=(signal,),
                    evaluated=("ema_trend",),
                    skipped=(),
                    diagnostics=(),
                    indicators=IndicatorSnapshot(
                        Timeframe.M15,
                        snap.last_price,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        0,
                    ),
                )

        class CaptureOrch(ResearchOrchestrator):
            def run(self, packet):
                self.last_packet = packet
                return super().run(packet)

        engine = IndexOptionsEngine(config)
        signal = StubStrategies().evaluate(snap).signals[0]
        options = engine.evaluate(signal, snap, chain)
        self.assertEqual(options.status, DecisionStatus.CANDIDATE)
        self.assertEqual(options.candidate.option_type, "CE")
        self.assertEqual(options.candidate.intent, "BUY")
        self.assertNotEqual(options.candidate.contract_symbol, options.candidate.candidate_id)

        ledger = BacktestLedger()
        orch = CaptureOrch(config)
        pipe = DecisionPipeline(
            hub=hub,
            strategies=StubStrategies(),
            options=engine,
            chains=chains,
            research=orch,
            simulator=ExecutionSimulator(SlippageModel(0), quantity=1),
            costs=CostModel(),
            ledger=ledger,
            run_id="2j-e2e",
            ablation="full",
            config_version=config.version,
            lot_size=1,
        )
        pipe.evaluate("NIFTY", as_of)
        self.assertEqual(len(ledger.trades), 1)
        trade = ledger.trades[0]
        self.assertEqual(trade.lot_size, 75)
        self.assertEqual(orch.last_packet.option_candidate["lot_size"], 75)
        self.assertEqual(trade.option_type, "CE")
        expected = contract_pnl(entry=trade.entry_fill, exit=trade.exit_fill, lots=1, lot_size=75)
        self.assertEqual(trade.gross_pnl, expected)
        self.assertNotEqual(
            trade.gross_pnl, contract_pnl(entry=trade.entry_fill, exit=trade.exit_fill, lots=1, lot_size=1)
        )

        runner_ctx = self.assertRaises(GrowConfigError)
        with runner_ctx as ctx:
            require_qualified_real(store, result.qualification)
        self.assertIn("NOT_APPROVED_FOR_2E", str(ctx.exception))
        with self.assertRaises(GrowConfigError) as ctx:
            open_qualified_runner(store, result.qualification, start=START, end=END, config=config)
        self.assertIn("NOT_APPROVED_FOR_2E", str(ctx.exception))

    def test_2f_recorded_sample_cannot_enter_approved_catalog(self) -> None:
        result = _ingest()
        self.assertEqual(result.qualification.fingerprint, result.store.meta.fingerprint)
        self.assertNotEqual(result.qualification.qualification_status, APPROVED_FOR_2E)
        director = FixtureDirector()
        with self.assertRaises(GrowConfigError) as ctx:
            bind_director_catalog(director, result.store, result.qualification)
        self.assertIn("NOT_APPROVED_FOR_2E", str(ctx.exception))
        with self.assertRaises(GrowConfigError):
            require_qualified_real(result.store, result.qualification)


class SafetyTests(unittest.TestCase):
    def test_no_network_or_broker_imports_in_integrate(self) -> None:
        self.assertFalse(LIVE_TRADING_COMPILED)
        root = pathlib.Path(__file__).resolve().parents[1] / "grow" / "history" / "integrate"
        banned = {"urllib", "requests", "httpx", "aiohttp", "websocket", "kiteconnect", "upstox", "dhanhq"}
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".", 1)[0]]
                for name in names:
                    self.assertNotIn(name, banned, msg=f"{path.name} imports {name}")

    def test_no_fixture_fallback_or_live_provider(self) -> None:
        raw = recorded_payload()
        raw["provider"] = "grow.history.sample.v1"
        with self.assertRaises(GrowConfigError) as ctx:
            ingest(recorded_scope(), payload=raw)
        self.assertIn("FIXTURE_FALLBACK_FORBIDDEN", str(ctx.exception))
        live_scope = AcquireScope(underlyings=("NIFTY",), start=START, end=END, provider_id="live.feed")
        with self.assertRaises(GrowConfigError) as ctx:
            ingest(live_scope, payload=recorded_payload())
        self.assertIn("PROVIDER_NOT_APPROVED", str(ctx.exception))
        cfg = historical_config()
        self.assertEqual(cfg.options.provider, "historical")
        self.assertNotEqual(cfg.options.provider, "live")
        self.assertEqual(cfg.backtest.provider, "fixture")


if __name__ == "__main__":
    unittest.main()
