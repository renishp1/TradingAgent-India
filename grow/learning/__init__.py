"""Learning / memory — interface only.

Persistent trade memory, reflection, and promoter/regime learning are deferred.
"""

from grow.errors import GrowInterfaceNotImplemented


class LearningStore:
    def remember(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise GrowInterfaceNotImplemented("grow.learning", "a later milestone")

    def recall(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise GrowInterfaceNotImplemented("grow.learning", "a later milestone")
