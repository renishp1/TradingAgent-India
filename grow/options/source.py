"""Provider-neutral option-chain contract. 2C constructs only the fixture."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from grow.data.schema import SourceMeta
from grow.options.models import OptionChainSnapshot


@runtime_checkable
class OptionChainSource(Protocol):
    def meta(self) -> SourceMeta: ...

    def snapshot(self, underlying: str, as_of: datetime, *, spot: float) -> OptionChainSnapshot: ...
