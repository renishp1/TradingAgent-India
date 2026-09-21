"""Grow exception hierarchy.

Safety errors are distinct from ordinary configuration or research errors so
callers can fail closed without guessing.
"""

from __future__ import annotations


class GrowError(Exception):
    """Base class for all Grow errors."""


class GrowConfigError(GrowError):
    """Invalid or forbidden configuration."""


class GrowSafetyError(GrowError):
    """A safety invariant was violated. Fail closed."""


class GrowLiveTradingDisabled(GrowSafetyError):
    """Live / broker execution was requested. Grow will not do that."""


class GrowRiskRejected(GrowError):
    """Risk Guard refused a proposal. The proposal must not reach any venue."""

    def __init__(self, reason: str, rule_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rule_id = rule_id


class GrowInterfaceNotImplemented(GrowError):
    """A milestone-2+ module was called. The interface exists; the desk does not."""

    def __init__(self, module: str, when: str = "a later milestone") -> None:
        super().__init__(f"{module} is not implemented until {when}.")
        self.module = module
        self.when = when
