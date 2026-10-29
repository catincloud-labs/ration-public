"""Exclusivity against Postgres from separate interpreters.

This is not a conformance test and declares no property. It decides nothing the
contract states, and no violator is expected to fail it -- `tests/test_the_suite_
discriminates.py` therefore never counts it. It exists to answer one question,
and to answer it with a measurement rather than a paragraph:

    the hammer is 8 threads in one interpreter, and a lease service's real
    population is processes on more than one host.

**What the answer is.** For *detection* the threads are already enough: every
operation is a real round trip, so the writers interleave without the
interpreter needing to be persuaded, and the threaded hammer in
`test_atomicity.py` is calibrated against `postgres-naive` there. This probe
does not catch anything the hammer misses, and was not expected to.

**It closes a structural gap rather than a detection one, and the gap is not
the thread count.** The threaded hammer shares **one `PostgresStore` instance**
across all eight workers. That instance holds a `threading.local()` connection
registry, so the threads are eight clients of one store object. A real
deployment is not that -- it is N processes each holding their own store, on
their own connection, with nothing in common but the database. Any defect that
lives in state accidentally shared *inside* a store instance is invisible to a
probe where there is only one instance to share it. This probe has one store per
process and therefore cannot inherit that blindness.

**It remains a probe.** It has no seam to park in either, so a run that finds
nothing has not shown there was nothing to find. What keeps it honest is the
second test below: the identical harness is pointed at `postgres-naive` and is
required to catch it. A probe that has never been shown to fail is a probe that
has been shown nothing.
"""

from __future__ import annotations

import multiprocessing as mp
import time

import pytest

from ration.contract import ABSENT, Applied, Record
from ration.postgres import PostgresStore
from tests.support.backends import POSTGRES_DSN
from tests.support.crossprocess import worker_result

pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN, reason="no Postgres configured; RATION_POSTGRES_DSN is unset"
)

#: Deliberately the same arithmetic as the threaded hammer in
#: `tests/conformance/test_atomicity.py` -- eight contenders, one hundred
#: increments each, eight hundred expected. The counts are matched so the two
#: verdicts are comparable without translation: if this probe and that hammer
#: ever disagree about the same store, the population is the only difference
#: between them.
PROCESSES = 8
ITERATIONS = 100

#: Generous, and it is a failure rather than a skip when it is exceeded. Process
#: startup under `spawn` is not free and a loaded machine is not a broken store,
#: but a probe allowed to give up quietly would report the same green as one
#: that finished.
JOIN_TIMEOUT = 300.0


def _seed(key: str) -> PostgresStore:
    store = PostgresStore(POSTGRES_DSN)
    store.ensure_schema()
    seeded = store.compare_and_set(key, ABSENT, Record(b"0", "seed"))
    assert isinstance(seeded, Applied), f"could not seed {key!r}: {seeded}"
    return store


def _contend(store_kind: str, key: str) -> tuple[int, set[int], float]:
    """Run the probe. Returns the final counter, the PIDs seen, and seconds."""
    payloads = [
        (POSTGRES_DSN, store_kind, key, ITERATIONS, n) for n in range(PROCESSES)
    ]
    started = time.monotonic()
    # `spawn` explicitly rather than the platform default: `fork` would inherit
    # the parent's connections and make "its own store from nothing but a DSN"
    # false in exactly the way this probe exists to avoid.
    with mp.get_context("spawn").Pool(processes=PROCESSES) as pool:
        results = pool.map_async(worker_result, payloads).get(timeout=JOIN_TIMEOUT)
    elapsed = time.monotonic() - started

    pids = {pid for pid, _ in results}
    errors = [err for _, err in results if err is not None]
    assert not errors, f"worker(s) failed: {errors}"

    final = PostgresStore(POSTGRES_DSN).read(key)
    assert final.record is not None
    return int(final.record.payload), pids, elapsed


def test_the_harness_really_used_separate_interpreters() -> None:
    """Calibrates the probe rather than the store, and must run first.

    A pool that quietly ran every task in the parent would produce a perfect
    counter and prove nothing at all -- the threaded hammer's result wearing
    this test's name. So the distinct-PID count is asserted before any verdict
    about a store is trusted.
    """
    key = f"xproc-harness-{time.time_ns()}"
    _seed(key)
    total, pids, _ = _contend("postgres", key)

    assert len(pids) == PROCESSES, (
        f"expected {PROCESSES} distinct worker processes and saw {len(pids)}: "
        f"{sorted(pids)}. A probe that did not fork is not this probe."
    )
    assert total == PROCESSES * ITERATIONS


def test_the_correct_store_loses_nothing_across_processes() -> None:
    key = f"xproc-correct-{time.time_ns()}"
    _seed(key)
    total, pids, elapsed = _contend("postgres", key)

    expected = PROCESSES * ITERATIONS
    assert total == expected, (
        f"{expected - total} increments were applied and then lost across "
        f"{len(pids)} processes: every one of them was reported to a caller as "
        f"having happened"
    )
    print(f"\npostgres: {total}/{expected} in {elapsed:.1f}s across {len(pids)} pids")


def test_the_naive_store_is_caught_across_processes() -> None:
    """The calibration, and the only reason the test above is worth anything.

    **This is not a conformance xfail.** `postgres-naive` is registered as a
    violator of `atomicity` in the suite proper, where a strict xfail carries
    that verdict. Here the requirement is stated as a plain assertion because
    the subject is *this harness*: if the naive store survives eight processes
    intact, the harness is not creating contention and its green against the
    correct store means nothing.
    """
    key = f"xproc-naive-{time.time_ns()}"
    _seed(key)
    total, pids, elapsed = _contend("postgres-naive", key)

    expected = PROCESSES * ITERATIONS
    assert total < expected, (
        f"postgres-naive kept all {expected} increments across {len(pids)} "
        f"processes. It has no version predicate in its write, so it cannot be "
        f"correct -- which means this harness produced no overlapping writes "
        f"and decides nothing about the store above."
    )
    print(
        f"\npostgres-naive: {total}/{expected} in {elapsed:.1f}s "
        f"({expected - total} lost) across {len(pids)} pids"
    )
