"""Model gateway — the only object agents use to talk to an LLM."""

from __future__ import annotations

from grow.config import GrowConfig
from grow.model_gateway.providers import ModelProvider, ModelRequest, ModelResponse, provider_from_name


class ModelGateway:
    def __init__(self, config: GrowConfig, provider: ModelProvider | None = None) -> None:
        self.config = config
        self.provider = provider or provider_from_name(config.model.provider)

    @property
    def name(self) -> str:
        return self.provider.name

    def complete(
        self,
        purpose: str,
        prompt: str,
        *,
        deep: bool = False,
        schema_hint: str | None = None,
    ) -> ModelResponse:
        model = self.config.model.deep_model if deep else self.config.model.fast_model
        request = ModelRequest(
            purpose=purpose,
            prompt=prompt,
            model=model,
            max_tokens=self.config.model.max_output_tokens,
            schema_hint=schema_hint,
        )
        return self.provider.complete(request)
