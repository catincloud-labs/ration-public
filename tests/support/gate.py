"""A parking spot inside a store, and the record that it was reached.

The record is the point. A threaded hammer can tell *that* it raced -- a
writer refused because another moved the version between its read and its
write can only have been refused by a genuine overlap -- but not *how*: which
interleaving it got is the scheduler's choice, so on a violator its catch is
a matter of chance. With this gate a test arranges the interleaving instead:
it parks one operation at a chosen point inside the store, runs another to
completion while the first waits, and can prove the interleaving it arranged
happened, from who arrived at the seam (`arrivals_by_thread`) and from
`NeverParked` when the park never fired. The price is reach: the point is a
seam in the store's own code, so the gate reaches only stores whose atomic
step is in this process.
"""

from __future__ import annotations

import threading
import time

__all__ = ["Gate", "NeverParked"]


class NeverParked(AssertionError):
    """The interleaving the test set up did not happen.

    Raised rather than returned. A stepper test whose park never fired has
    measured nothing, and the one thing it must not do is pass quietly.
    """


class Gate:
    """Parks one nominated thread at a store's `_staged` seam; passes the rest.

    Nomination is by thread rather than by arrival order because both threads in
    an interleaving reach the seam, and which of them arrives first is the thing
    under test rather than something the harness may assume.
    """

    def __init__(self) -> None:
        self._parked = threading.Event()
        self._released = threading.Event()
        self._lock = threading.Lock()
        self._nominated: threading.Thread | None = None
        #: Counted per thread, not in total. A store may have a seam on more
        #: than one code path, so "the seam was reached twice" says nothing
        #: about *who* reached it -- and who is the entire question when the
        #: assertion is that a second operation got into the store.
        self.arrivals_by_thread: dict[str, int] = {}

    def park_thread(self, thread: threading.Thread) -> None:
        """Nominate the thread to stop. Must be called before it is started."""
        self._nominated = thread

    def arrive(self) -> None:
        """Called from inside the store. Blocks the nominated thread only."""
        current = threading.current_thread()
        with self._lock:
            self.arrivals_by_thread[current.name] = (
                self.arrivals_by_thread.get(current.name, 0) + 1
            )
            mine = current is self._nominated
        if mine:
            self._parked.set()
            # Bounded so a suite that never releases fails as a slow test
            # rather than hanging a CI run until the job timeout.
            self._released.wait(timeout=10.0)

    def wait_until_parked(self, timeout: float = 10.0) -> None:
        if not self._parked.wait(timeout):
            raise NeverParked(
                "the operation never reached the staging seam, so the "
                "interleaving under test never happened"
            )

    def wait_until_parked_or_finished(
        self, thread: threading.Thread, timeout: float = 10.0
    ) -> bool:
        """True if the thread parked, False if it finished without reaching the seam.

        Not every store has a seam on every code path, and a store with no
        window on a path is not obliged to invent one. So this reports which
        happened rather than insisting, and the caller asserts only what it
        actually observed.

        A conditional assertion can stop firing without anyone noticing,
        which is why the implementation that *must* park is a registered
        violator: if it stops parking, its expected failure becomes a pass, and
        strict xfail turns that into a red run.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._parked.is_set():
                return True
            if not thread.is_alive():
                return False
            time.sleep(0.001)
        raise NeverParked("the thread neither parked nor finished")

    def release(self) -> None:
        self._released.set()
