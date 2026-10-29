"""Two clocks, because property 2 is a question about which one is consulted.

A test that moves time by sleeping cannot tell the difference between a store
that evaluates expiry itself and one that trusts whatever the caller worked out,
because when both clocks advance together every implementation looks correct.
Skew is the whole experiment, so the clocks have to be separable.
"""

from __future__ import annotations

__all__ = ["FakeClock"]


class FakeClock:
    """A clock that moves only when told to.

    The starting value is arbitrary and non-zero: a store that forgets to record
    a deadline gets `None`, and a store that records one from a clock reading
    zero looks the same as one that recorded nothing.
    """

    def __init__(self, now: float = 1_000_000.0) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds
