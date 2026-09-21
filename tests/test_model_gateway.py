from __future__ import annotations

import unittest

from grow.config import load_config
from grow.errors import GrowConfigError, GrowInterfaceNotImplemented
from grow.model_gateway import ModelGateway, ModelRequest, provider_from_name


class ModelGatewayTests(unittest.TestCase):
    def test_mock_is_default(self) -> None:
        gateway = ModelGateway(load_config())
        self.assertEqual(gateway.name, "mock")
        response = gateway.complete("ceo.propose", "probe")
        self.assertIn("side", response.structured)
        self.assertEqual(response.provider, "mock")

    def test_unknown_provider(self) -> None:
        with self.assertRaises(GrowConfigError):
            provider_from_name("not-a-vendor")

    def test_remote_provider_does_not_complete(self) -> None:
        provider = provider_from_name("xai")
        request = ModelRequest(purpose="x", prompt="p", model="m", max_tokens=8)
        with self.assertRaises((GrowConfigError, GrowInterfaceNotImplemented)):
            provider.complete(request)
