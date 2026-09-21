"""Model providers.

The mock provider is the default and the only one used by tests. Remote
providers exist as interfaces: they refuse to run without an API key and
are not called by the paper cycle in milestone 1.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from grow.errors import GrowConfigError, GrowInterfaceNotImplemented


@dataclass(frozen=True)
class ModelRequest:
    purpose: str
    prompt: str
    model: str
    max_tokens: int
    schema_hint: str | None = None


@dataclass(frozen=True)
class ModelResponse:
    provider: str
    model: str
    text: str
    structured: dict[str, Any]
    usage_tokens: int


class ModelProvider(Protocol):
    name: str

    def complete(self, request: ModelRequest) -> ModelResponse: ...


class MockProvider:
    name = "mock"

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.purpose == "ceo.propose":
            payload = {
                "action": "OPEN",
                "side": "BUY",
                "quantity": 1,
                "confidence": 0.42,
                "thesis": "Mock CEO: size-one probe only. Not a forecast.",
            }
        elif request.purpose == "research.summarize":
            payload = {
                "summary": "Mock research summary. No live market data was used.",
                "bias": "neutral",
            }
        else:
            payload = {"echo": request.prompt[:200]}
        return ModelResponse(
            provider=self.name,
            model=request.model,
            text=json.dumps(payload, indent=2),
            structured=payload,
            usage_tokens=len(request.prompt.split()),
        )


class EnvKeyedProvider:
    """Stub for a remote LLM. Refuses unless the key is present, then still
    raises NotImplemented — remote calls are a later milestone."""

    def __init__(self, name: str, env_var: str) -> None:
        self.name = name
        self.env_var = env_var

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not os.environ.get(self.env_var):
            raise GrowConfigError(f"{self.name} requires {self.env_var} to be set.")
        raise GrowInterfaceNotImplemented(f"grow.model_gateway.{self.name}", "a later milestone")


def provider_from_name(name: str) -> ModelProvider:
    key = name.strip().lower()
    if key in {"mock", "none", "deterministic"}:
        return MockProvider()
    mapping = {
        "xai": ("xai", "XAI_API_KEY"),
        "openai": ("openai", "OPENAI_API_KEY"),
        "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
        "ollama": ("ollama", "OLLAMA_HOST"),
    }
    if key not in mapping:
        raise GrowConfigError(f"Unknown model provider {name!r}")
    provider_name, env_var = mapping[key]
    return EnvKeyedProvider(provider_name, env_var)
