"""An in-memory store that satisfies the contract, used to prove the suite passes.

This is **not a backend**. Nothing here crosses a process boundary, so it cannot
demonstrate the thing a real backend has to demonstrate, and a suite that has
only ever run against it has shown that the suite can pass and nothing else.

Its job is the other half of discrimination. The violating implementations prove
the suite can reject; this one proves the suite is not simply rejecting
everything -- a suite that fails all comers is exactly as uninformative as one
that passes all comers, and only the pair distinguishes them.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ration.contract import (
    ABSENT,
    Applied,
    Conflict,
    Outcome,
    Reading,
    Record,
)

__all__ = ["MemoryStore"]


class MemoryStore:
    """Correct by construction: one lock, and a version counter that only rises.

    The two structures are separate on purpose. `_values` holds what is
    currently there; `_versions` holds how far each key has *ever* got, and a
    key is never removed from it. Deletion therefore cannot recycle a version,
    because the number deletion would have to reuse is not stored with the value
    it deleted.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        #: The store's own clock. Property 2 is about whose clock this is: it
        #: belongs to the store, is never supplied per-write, and no caller's
        #: idea of the time reaches it.
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()
        self._versions: dict[str, int] = {}
        self._values: dict[str, tuple[Record, float | None]] = {}

    # -- the contract -----------------------------------------------------

    def read(self, key: str) -> Reading:
        with self._lock:
            return self._read_locked(key)

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        record: Record | None,
        *,
        ttl: float | None = None,
    ) -> Outcome:
        self._staged()
        with self._lock:
            reading = self._read_locked(key)
            if reading.version != expected_version:
                # Reported without writing. The version returned is the one the
                # key actually reached, because one lock covers the comparison
                # and the report. The contract does not promise that of every
                # store, so a caller that needs the current version still reads.
                return Conflict(reading.version)
            return self._commit_locked(key, record, ttl)

    # -- the parts a violating implementation replaces --------------------
    #
    # Each of the three below is the whole of one property. That is not a
    # coincidence: they were factored out because a violator should be able to
    # break exactly one thing, and a violation that has to be spelled across
    # three methods is one nobody can check is targeted.

    def _expiry_clock(self) -> float:
        """The clock expiry is evaluated against. Property 2 says whose."""
        return self._clock()

    def _read_locked(self, key: str) -> Reading:
        version = self._versions.get(key, ABSENT)
        held = self._values.get(key)
        if held is None:
            return Reading(version, None)
        record, deadline = held
        if deadline is not None and self._expiry_clock() >= deadline:
            # Evaluated here, on read, against the store's clock. The entry is
            # left in place rather than reaped: expiry is a predicate over the
            # clock, and a background reaper would make it a race instead.
            return Reading(version, None)
        return Reading(version, record)

    def _commit_locked(
        self, key: str, record: Record | None, ttl: float | None
    ) -> Applied:
        version = self._versions.get(key, ABSENT) + 1
        self._versions[key] = version
        if record is None:
            self._values.pop(key, None)
        else:
            deadline = None if ttl is None else self._expiry_clock() + ttl
            self._values[key] = (record, deadline)
        return Applied(version)

    # -- the seam the suite parks in --------------------------------------

    def _staged(self) -> None:
        """The last moment before this implementation's atomic step. A no-op.

        Its position is the claim being made, and the claim here is a strong
        one: *nothing has been read yet*. Everything this call will act on is
        fetched after it returns, inside the lock, so an operation stopped here
        for an arbitrarily long time resumes with no stale belief to act on.

        That is what makes the suite's interleaving deterministic rather than
        lucky. A second writer parked here cannot deadlock a first, because no
        lock is held; it runs to completion, and the assertion is about what the
        two of them did rather than about whether they raced in time. A store
        whose seam sits *after* a read it will later act on has confessed to
        having a window, and the suite does not have to catch it in one.

        This is not a contract clause. Nothing requires a backend to have a seam
        -- a real store's atomic step happens on the far side of a socket, where
        no client-side stepper can reach it. That is what the suite's threaded
        probe is for, and why the probe is not the only check.
        """
