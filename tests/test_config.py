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
        self.assertEqual(config.market.product, "CASH")
        self.assertFalse(config.risk.allow_short)
        self.assertEqual(config.risk.concentration_basis, "cost_notional")
        self.assertEqual(config.data.provider, "fixture")
        self.assertFalse(config.data.allow_live_feed)
        self.assertEqual(config.strategies.universe, ("NIFTY", "BANKNIFTY"))
        self.assertEqual(config.strategies.primary_timeframe, "M15")
        self.assertEqual(config.strategies.supported_timeframes, ("M5", "M15", "D1"))
        self.assertEqual(config.options.provider, "fixture")
        self.assertFalse(config.options.allow_same_day)
        self.assertEqual(config.options.preferred_expiry_class, "weekly")
        self.assertEqual(config.options.max_distance_from_atm, 2)
        self.assertEqual(config.options.max_quote_age_minutes, 5)
        self.assertEqual(config.ai.provider, "fixture")
        self.assertFalse(config.ai.allow_ai_execution)
        self.assertFalse(config.ai.allow_broker)
        self.assertEqual(config.backtest.provider, "fixture")
        self.assertEqual(config.backtest.fill_model, "ask_plus_slippage")
        self.assertEqual(config.backtest.lot_size, 1)
        self.assertFalse(config.backtest.calibrate_on_test)


    def test_live_yaml_value_cannot_pass_through_env_truth(self) -> None:
        with self.assertRaises(GrowLiveTradingDisabled):
            load_config(environ={"LIVE_TRADING_ENABLED": "yes"})
