"""TradingAgents foundation for Grow.

Clean-room contracts inspired by TauricResearch/TradingAgents (Apache-2.0).
This package is not a copy of that repository. It provides:

- agent role protocols (analyst, researcher, trader)
- a deterministic sequential research graph
- a default_config mapping Grow can consume

LangGraph, vendor dataflows, and upstream LLM clients are intentionally absent.
See NOTICE and docs/architecture.md.
"""

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.research_graph import ResearchBundle, ResearchGraph

__version__ = "0.1.0-foundation"

__all__ = ["DEFAULT_CONFIG", "ResearchBundle", "ResearchGraph", "__version__"]
