"""Market-data provider adapters.

Live stream adapters remain in ``grow.live_data``. This package is reserved for
provider-neutral wiring only. Broker SDKs must not be imported here.
"""

# Providers are registered by the live_data layer after smoke validation.
APPROVED_AGENT_PROVIDERS = frozenset(
    {
        "grow.stream.mock.v1",
        "grow.stream.truedata.v1",
        "grow.fixture.agent.v1",
    }
)
