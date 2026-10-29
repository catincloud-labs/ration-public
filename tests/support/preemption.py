"""Making CPython switch threads finely enough to land in a microsecond window.

Shared by `tests/conformance/test_atomicity.py` and the concurrency probes of
the lease, pool and publish tests, and shared rather than copied because two
copies of a calibration is one calibration and one stale number. The *counts*
it was calibrated with stay in `test_atomicity.py`, where they were measured;
only the mechanism is shared.

This does not widen anybody's window. Nothing is inserted into any
implementation and no gap is made longer. It makes the interpreter sample finely
enough to land inside a gap that was always there -- which is the whole reason a
probe using it can be trusted to have looked.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["PREEMPTION_INTERVAL", "preemptive"]

#: Calibrated against the naive store: the value at which a known-bad
#: implementation fails every run rather than most runs.
#:
#: The rates are in `tests/conformance/test_atomicity.py`, where they were
#: measured, and are deliberately not repeated here.
PREEMPTION_INTERVAL = 1e-6


@contextmanager
def preemptive(interval: float = PREEMPTION_INTERVAL) -> Iterator[None]:
    """Switch threads every `interval` seconds for the duration of the block.

    Process-wide, so it is restored on the way out. A leaked switch interval
    would slow every later test and, worse, would make a calibration depend on
    which tests happened to run before it.
    """
    previous = sys.getswitchinterval()
    sys.setswitchinterval(interval)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)
