"""Making a store unreachable, and making a write's outcome unknown.

Properties 3 and 4 are both about what a caller does when the store stops
answering, so neither can be tested by a store that always answers. The faults
sit in front of the store rather than inside it, which is also how they would be
injected against a real backend -- a proxy that drops the connection is
available for anything reachable over a socket, where reaching into the
implementation is not.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from ration.contract import Ambiguous, Outcome, Reading, Record, Store, Unreachable

__all__ = ["FaultInjector"]


class FaultInjector:
    """A transparent proxy that can be told to stop being transparent."""

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self._unreachable = False
        self._ambiguous_next: bool | None = None

    # -- the contract, delegated ------------------------------------------

    def read(self, key: str) -> Reading:
        if self._unreachable:
            raise Unreachable(key)
        return self._inner.read(key)

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        record: Record | None,
        *,
        ttl: float | None = None,
    ) -> Outcome:
        if self._unreachable:
            raise Unreachable(key)
        if self._ambiguous_next is False:
            # Lost before the store saw it. The write did not happen, and the
            # caller cannot know that without asking.
            self._ambiguous_next = None
            raise Ambiguous(key)
        outcome = self._inner.compare_and_set(
            key, expected_version, record, ttl=ttl
        )
        if self._ambiguous_next is True:
            # Applied, and then the answer was lost on the way back. This is
            # the direction that actually costs something: a caller that
            # assumes failure here retries a write it already made.
            self._ambiguous_next = None
            raise Ambiguous(key)
        return outcome

    # -- the faults --------------------------------------------------------

    @contextmanager
    def partition(self) -> Iterator[None]:
        """The store's node is gone, or the network to it is down: both are a partition."""
        self._unreachable = True
        try:
            yield
        finally:
            self._unreachable = False

    @contextmanager
    def ambiguity(self, *, applied: bool) -> Iterator[None]:
        """The next write raises `Ambiguous`, having done what `applied` says.

        Both directions are exercised by the suite, and they are what stops a
        resolver that always answers the same way from looking correct.
        """
        self._ambiguous_next = applied
        try:
            yield
        finally:
            self._ambiguous_next = None
