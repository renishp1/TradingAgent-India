from __future__ import annotations

import unittest
from pathlib import Path

from grow.config import load_config, parse_simple_yaml
from grow.errors import GrowLiveTradingDisabled


class ConfigTests(unittest.TestCase):
    def test_default_yaml_roundtrip(self) -> None:
        path = Path(__file__).resolve().parents[1] / "configs" / "grow.default.yaml"
        raw = parse_simple_yaml(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["grow"]["execution"]["mode"], "paper")
        self.assertFalse(raw["grow"]["execution"]["live_trading_enabled"])
        self.assertIn("RELIANCE", raw["grow"]["market"]["universe"])

    def test_env_overlay_cash(self) -> None:
        config = load_config(environ={"GROW_STARTING_CASH": "250000"})
        self.assertEqual(config.paper.starting_cash, 250000)

    def test_square_off_is_before_close(self) -> None:
        config = load_config()
        self.assertEqual(config.market.square_off, "15:15")
        self.assertEqual(config.market.session_close, "15:30")
        self.assertEqual(config.timezone, "Asia/Kolkata")

    def test_live_yaml_value_cannot_pass_through_env_truth(self) -> None:
        with self.assertRaises(GrowLiveTradingDisabled):
            load_config(environ={"LIVE_TRADING_ENABLED": "yes"})
