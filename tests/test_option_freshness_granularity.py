"""Hardening: per-option freshness must not auto-stale the whole snapshot."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from grow.agents.options import OptionsChainAgent
from grow.clock import IST
from grow.decision.contracts.agent_result import AgentStatus, CandidateAction
from grow.live_data.mock import bullish_event
from grow.live_data.normalize import normalize_event
from grow.market.session import SessionCalendar
from grow.market_data.normalized.models import DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import (
    build_agent_snapshot,
    build_fixture_snapshot,
    gate_snapshot_quality,
    option_contract_quality,
)
from grow.config import load_config


AS_OF = datetime(2026, 9, 22, 11, 0, tzinfo=IST)


def _option(
    *,
    contract_id: str,
    quality: DataQualityStatus,
    age: float = 0.0,
    strike: float = 25000.0,
) -> OptionQuoteView:
    return OptionQuoteView(
        underlying="NIFTY",
        expiry=AS_OF.date(),
        strike=strike,
        option_type="CE",
        ltp=120.0,
        bid=119.0,
        ask=121.0,
        open_interest=100,
        volume=10,
        quote_timestamp=AS_OF - timedelta(seconds=age),
        quote_age_seconds=age,
        provider_contract_id=contract_id,
        quality=quality,
    )


class PerOptionFreshnessTests(unittest.TestCase):
    def test_option_contract_quality_helper(self) -> None:
        self.assertEqual(
            option_contract_quality(age_seconds=10.0, max_quote_age_seconds=30.0),
            DataQualityStatus.OK,
        )
        self.assertEqual(
            option_contract_quality(age_seconds=31.0, max_quote_age_seconds=30.0),
            DataQualityStatus.STALE,
        )

    def test_one_stale_option_does_not_stale_snapshot(self) -> None:
        stale = _option(contract_id="stale-1", quality=DataQualityStatus.STALE, age=90.0)
        fresh_a = _option(contract_id="fresh-1", quality=DataQualityStatus.OK, strike=25100.0)
        fresh_b = _option(contract_id="fresh-2", quality=DataQualityStatus.OK, strike=24900.0)
        snap = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=AS_OF,
            spot=25000.0,
            option_contracts=(stale, fresh_a, fresh_b),
            quality=DataQualityStatus.OK,
        )
        self.assertEqual(snap.data_quality, DataQualityStatus.OK)
        self.assertEqual(gate_snapshot_quality(snap), DataQualityStatus.OK)
        self.assertEqual(snap.option_contracts[0].quality, DataQualityStatus.STALE)
        self.assertEqual(snap.option_contracts[1].quality, DataQualityStatus.OK)
        result = OptionsChainAgent().analyze(snap)
        self.assertEqual(result.status, AgentStatus.DEGRADED)
        self.assertEqual(result.candidate_action, CandidateAction.NONE)
        self.assertTrue(any("unusable:stale-1" in row or "stale:stale-1" in row for row in (*result.observations, *result.data_quality_concerns)))
        self.assertTrue(any(row.startswith("usable=2") for row in result.observations))
        self.assertIn("STALE_OPTIONS_EXCLUDED", result.risk_flags)

    def test_all_options_stale(self) -> None:
        options = (
            _option(contract_id="s1", quality=DataQualityStatus.STALE, age=90.0),
            _option(contract_id="s2", quality=DataQualityStatus.STALE, age=120.0, strike=25100.0),
        )
        snap = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=AS_OF,
            spot=25000.0,
            option_contracts=options,
            quality=DataQualityStatus.OK,
        )
        self.assertEqual(gate_snapshot_quality(snap), DataQualityStatus.OK)
        result = OptionsChainAgent().analyze(snap)
        self.assertEqual(result.status, AgentStatus.NO_DATA)
        self.assertIn("usable_option_contracts", result.missing_data)
        self.assertEqual(result.candidate_action, CandidateAction.NONE)

    def test_fresh_underlying_with_stale_option(self) -> None:
        snap = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=AS_OF,
            spot=25000.0,
            option_contracts=(
                _option(contract_id="stale", quality=DataQualityStatus.STALE, age=99.0),
                _option(contract_id="fresh", quality=DataQualityStatus.OK, strike=25100.0),
            ),
            quality=DataQualityStatus.OK,
        )
        self.assertEqual(snap.underlyings["NIFTY"].ltp, 25000.0)
        self.assertEqual(snap.data_quality, DataQualityStatus.OK)
        self.assertEqual(gate_snapshot_quality(snap), DataQualityStatus.OK)

    def test_stale_underlying_fails_snapshot_gate(self) -> None:
        snap = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=AS_OF,
            spot=25000.0,
            option_contracts=(_option(contract_id="fresh", quality=DataQualityStatus.OK),),
            quality=DataQualityStatus.STALE,
            notes=("STALE_UNDERLYING:NIFTY",),
        )
        self.assertEqual(gate_snapshot_quality(snap), DataQualityStatus.STALE)
        result = OptionsChainAgent().analyze(snap)
        self.assertEqual(result.status, AgentStatus.NO_DATA)

    def test_missing_underlying(self) -> None:
        snap = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=AS_OF,
            spot=25000.0,
            include_underlying=False,
            option_contracts=(_option(contract_id="orphan", quality=DataQualityStatus.OK),),
        )
        self.assertEqual(snap.data_quality, DataQualityStatus.INSUFFICIENT)
        self.assertEqual(gate_snapshot_quality(snap), DataQualityStatus.INSUFFICIENT)
        self.assertEqual(len(snap.underlyings), 0)

    def test_build_agent_snapshot_keeps_fresh_options_when_one_is_stale(self) -> None:
        calendar = SessionCalendar(load_config().market)
        live = normalize_event(
            bullish_event(as_of=AS_OF, underlyings=("NIFTY",)),
            now=AS_OF,
            max_staleness_seconds=30,
            calendar=calendar,
        )
        self.assertTrue(live.freshness_ok)
        chain = live.chains["NIFTY"]
        self.assertGreaterEqual(len(chain.contracts), 2)
        stale_contract = replace(
            chain.contracts[0],
            timestamp=AS_OF - timedelta(seconds=120),
        )
        fresh_contracts = chain.contracts[1:]
        patched = replace(live, chains={"NIFTY": replace(chain, contracts=(stale_contract, *fresh_contracts))})
        agent_snap = build_agent_snapshot(
            patched,
            decision_timestamp=AS_OF,
            max_quote_age_seconds=30.0,
        )
        self.assertEqual(agent_snap.data_quality, DataQualityStatus.OK)
        qualities = {row.provider_contract_id: row.quality for row in agent_snap.option_contracts}
        self.assertEqual(qualities[stale_contract.provider_contract_id], DataQualityStatus.STALE)
        fresh_ok = [
            row
            for row in agent_snap.option_contracts
            if row.provider_contract_id != stale_contract.provider_contract_id
        ]
        self.assertTrue(fresh_ok)
        self.assertTrue(all(row.quality is DataQualityStatus.OK for row in fresh_ok))
        self.assertTrue(any("STALE_OPTION:" in note for note in agent_snap.quality_notes))


if __name__ == "__main__":
    unittest.main()
