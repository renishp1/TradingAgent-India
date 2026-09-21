"""Foundation default config.

Keys overlap in spirit with upstream TradingAgents so a later adapter can map
them. Values are Grow-safe: mock models, no live vendors, India timezone.
"""

from __future__ import annotations

DEFAULT_CONFIG: dict = {
    "llm_provider": "mock",
    "deep_think_llm": "mock-deep",
    "quick_think_llm": "mock-fast",
    "backend_url": None,
    "temperature": 0.0,
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "output_language": "English",
    "checkpoint_enabled": False,
    "timezone": "Asia/Kolkata",
    "benchmark_ticker": "NIFTY",
    "data_vendors": {
        "core_stock_apis": "stub",
        "technical_indicators": "stub",
        "fundamental_data": "stub",
        "news_data": "stub",
        "macro_data": "stub",
    },
}
