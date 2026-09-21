"""Safety tests: live trading cannot be enabled."""

from __future__ import annotations

import unittest
from pathlib import Path

from grow.config import load_config
from grow.errors import GrowLiveTradingDisabled
from grow.execution.live import LiveBroker, place_live_order
from grow.execution.lock import LIVE_TRADING_COMPILED, inspect_environment, normalize_execution_mode
from grow.types import Venue

ROOT = Path(__file__).resolve().parents[1]


class PaperLockTests(unittest.TestCase):
    def test_compiled_flag_is_false(self) -> None:
        self.assertIs(LIVE_TRADING_COMPILED, False)

    def test_source_does_not_set_compiled_true(self) -> None:
        lock_src = (ROOT / "grow" / "execution" / "lock.py").read_text(encoding="utf-8")
        self.assertIn("LIVE_TRADING_COMPILED = False", lock_src)
        self.assertNotIn("LIVE_TRADING_COMPILED = True", lock_src)

    def test_mode_live_rejected(self) -> None:
        with self.assertRaises(GrowLiveTradingDisabled):
            normalize_execution_mode("live")
        with self.assertRaises(GrowLiveTradingDisabled):
            normalize_execution_mode("kite")
        self.assertEqual(normalize_execution_mode("paper"), "paper")

    def test_env_live_flag_refuses_boot(self) -> None:
        with self.assertRaises(GrowLiveTradingDisabled):
            inspect_environment({"GROW_LIVE_TRADING": "true"})
        with self.assertRaises(GrowLiveTradingDisabled):
            inspect_environment({"LIVE_TRADING_ENABLED": "1"})
        with self.assertRaises(GrowLiveTradingDisabled):
            inspect_environment({"GROW_EXECUTION_MODE": "live"})
        with self.assertRaises(GrowLiveTradingDisabled):
            inspect_environment({"KITE_ACCESS_TOKEN": "abc"})

    def test_config_live_flag_refuses_boot(self) -> None:
        with self.assertRaises(GrowLiveTradingDisabled):
            load_config(environ={"GROW_EXECUTION_MODE": "live"})
        with self.assertRaises(GrowLiveTradingDisabled):
            load_config(environ={"GROW_LIVE_TRADING": "true"})

    def test_live_broker_cannot_be_constructed(self) -> None:
        with self.assertRaises(GrowLiveTradingDisabled):
            LiveBroker()
        with self.assertRaises(GrowLiveTradingDisabled):
            place_live_order(symbol="RELIANCE", qty=1)

    def test_venue_enum_has_no_live(self) -> None:
        names = {item.name for item in Venue}
        self.assertEqual(names, {"PAPER"})
        self.assertNotIn("LIVE", names)

    def test_default_config_loads_paper_only(self) -> None:
        config = load_config(environ={"GROW_EXECUTION_MODE": "paper"})
        self.assertEqual(config.execution.mode, "paper")
        self.assertFalse(config.execution.live_trading_enabled)


class TreeHygieneTests(unittest.TestCase):
    def test_no_broker_sdk_imports(self) -> None:
        forbidden = ("kiteconnect", "upstox", "dhanhq", "smartapi", "zerodha", "angelone")
        hits: list[str] = []
        for path in (ROOT / "grow").rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                if f"import {token}" in text or f"from {token}" in text:
                    hits.append(f"{path}: {token}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
