"""Tests for provider-neutral agent market snapshots."""

from __future__ import annotations

import unittest
from datetime import datetime
from types import MappingProxyType

from grow.clock import IST
from grow.market_data.normalized.models import AgentMarketSnapshot, DataQualityStatus, OptionQuoteView
from grow.market_data.snapshots.builder import build_fixture_snapshot, gate_snapshot_quality


class AgentSnapshotTests(unittest.TestCase):
    def test_fixture_snapshot_is_immutable_and_versioned(self) -> None:
        as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        snap = build_fixture_snapshot(underlying="NIFTY", as_of=as_of, spot=25000.0)
        self.assertTrue(snap.snapshot_id.startswith("agent-"))
        self.assertEqual(snap.schema, "grow.agent.market_snapshot.v1")
        self.assertIsInstance(snap.underlyings, MappingProxyType)
        self.assertTrue(snap.paper_mode)
        self.assertFalse(snap.live_trading)
        with self.assertRaises(TypeError):
            snap.underlyings["HACK"] = snap.underlyings["NIFTY"]  # type: ignore[index]
        again = build_fixture_snapshot(underlying="NIFTY", as_of=as_of, spot=25000.0)
        self.assertEqual(snap.version, again.version)
        self.assertEqual(snap.snapshot_id, again.snapshot_id)

    def test_live_trading_flag_rejected(self) -> None:
        as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        base = build_fixture_snapshot(underlying="NIFTY", as_of=as_of, spot=25000.0)
        with self.assertRaises(ValueError):
            AgentMarketSnapshot(
                snapshot_id=base.snapshot_id,
                version=base.version,
                schema=base.schema,
                provider=base.provider,
                exchange=base.exchange,
                session_timestamp=base.session_timestamp,
                decision_timestamp=base.decision_timestamp,
                session_date=base.session_date,
                underlyings=dict(base.underlyings),
                option_contracts=base.option_contracts,
                data_quality=base.data_quality,
                quality_notes=base.quality_notes,
                source_snapshot_ids=dict(base.source_snapshot_ids),
                diagnostics=dict(base.diagnostics),
                paper_mode=True,
                live_trading=True,
            )

    def test_stale_quality_gate(self) -> None:
        as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        snap = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=as_of,
            spot=25000.0,
            quality=DataQualityStatus.STALE,
            notes=("stale fixture",),
        )
        self.assertEqual(gate_snapshot_quality(snap), DataQualityStatus.STALE)

    def test_point_in_time_option_quotes_reference_snapshot(self) -> None:
        as_of = datetime(2026, 9, 22, 11, 0, tzinfo=IST)
        option = OptionQuoteView(
            underlying="NIFTY",
            expiry=as_of.date(),
            strike=25000.0,
            option_type="CE",
            ltp=100.0,
            bid=99.0,
            ask=101.0,
            open_interest=10,
            volume=5,
            quote_timestamp=as_of,
            quote_age_seconds=0.0,
            provider_contract_id="x",
            quality=DataQualityStatus.OK,
        )
        snap = build_fixture_snapshot(
            underlying="NIFTY",
            as_of=as_of,
            spot=25000.0,
            option_contracts=(option,),
        )
        self.assertEqual(len(snap.option_contracts), 1)
        self.assertEqual(snap.option_contracts[0].quote_timestamp, as_of)
        payload = snap.to_dict()
        self.assertEqual(payload["snapshot_id"], snap.snapshot_id)
        self.assertFalse(payload["live_trading"])


if __name__ == "__main__":
    unittest.main()
