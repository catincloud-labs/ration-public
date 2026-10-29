"""Property: the comparison and the write are one step, not two.

Violated by `naive` and by `conflict-writes`.

Two checks, doing different jobs:

- **The stepper is the gate.** It parks one writer at the last moment before
  its atomic step, runs another to completion, and asserts that the
  interleaving actually happened. Nothing about it is timing-dependent: the
  seam is outside every lock, so the parked writer blocks nobody and the second
  writer is *waited for* rather than given a window and assumed. It reaches
  only stores whose atomic step is in this process.
- **The hammer is a probe.** Real threads against a real store, reaching
  non-atomicity no client-side stepper can. It carries a false-pass rate, stated
  below rather than implied, and it is not what the suite rests on.

  **A hammer can tell that it raced, and not whether it raced inside a
  violator's read-to-write window.** The race leaves a
  trace: a writer refused because another moved the version between its read
  and its write can only have been refused by a genuine overlap. So
  `test_concurrent_increments_lose_nothing` counts refusals and reports *"the
  probe never raced"* instead of a verdict when there were none.

  **Counting them is not sufficient on its own, and the order is the
  control.** A store that lets both writers through produces **no** refusals at
  all, so on a violator the count is legitimately zero -- reading it first would
  turn a caught violation into an inconclusive run. The lost-increment check is
  therefore decided first, because a lost increment is itself proof the window
  was reached.

  **And on a violator the hammer's catch is probabilistic, which would make
  the control that guards "the property is not being tested" fire on quiet
  runners.** `naive` is a registered violator, so its strict xfail is what
  turns a missed race into a red run - the right design, at a price: a hammer
  that raced (refusals seen) without landing inside the read-to-write window
  loses nothing, the xfail passes, and CI is red on a commit that changed
  nothing it tests. A red that a re-run clears is a red people learn to
  re-run. So where a store has a seam,
  `test_concurrent_increments_lose_nothing` decides the lost increment
  **through the seam first**: one incrementer parked between its read and its
  write, a second run to completion, the first released. A store whose
  comparison and write are one step refuses the stale writer and the counter
  reads two; the naive store applies it and the counter reads one, every run.
  The hammer follows as the probe, and it is the only instrument on a store
  with no seam.
"""

from __future__ import annotations

import threading

import pytest

from ration.contract import ABSENT, Applied, Record, Store
from tests.support.backends import BackendUnderTest
from tests.support.gate import Gate
from tests.support.preemption import preemptive

#: Calibrated against the naive store, because that is the only thing that can
#: calibrate it: the counts are whatever makes the known-bad implementation fail
#: every run.
#:
#: One calibration is the finding worth keeping: at these counts and the
#: interpreter's default switch interval the hammer caught the naive store
#: **0 times in 100 runs** -- it ran green, against the implementation the
#: atomicity property exists to reject. The window between a naive read and
#: its write is around a microsecond and the interpreter was sampling every five
#: milliseconds, so the probe was not unlucky; it was not looking.
#:
#: With preemption forced to a microsecond (`tests/support/preemption.py`) the
#: same counts caught it **500 times in 500 runs**, at about 8ms a run. That is
#: the stated rate: no false pass observed in 500 calibration runs, which is a
#: measurement and not a guarantee -- it is why this probe is not what the
#: property rests on.
#:
#: Against `postgres-naive` the same counts caught it **20 times in 20 runs**,
#: and that number carries more weight than the 500 does, because it is the one
#: case where this probe is the *only* instrument -- a store across a socket has
#: no seam for the stepper. It is also the easier catch: every operation is a
#: real round trip, so the writers genuinely interleave rather than needing the
#: interpreter to be persuaded to switch.
HAMMER_THREADS = 8
HAMMER_ITERATIONS = 100

#: The interval itself lives in `tests/support/preemption.py`, because the
#: lease service's concurrency probe needs the same mechanism. The counts above
#: stay here, with the run that measured them.

#: A retry budget, not a timeout. An unbounded retry loop against a store that
#: never applies is a hung CI job reported as nothing at all.
MAX_RETRIES = 10_000


@pytest.mark.conformance("atomicity")
def test_two_writers_racing_from_one_version_do_not_both_apply(
    backend: BackendUnderTest,
) -> None:
    gate = Gate()
    stepped = backend.stepped(gate)
    if stepped is None:
        # No in-process seam. For a real store that is evidence rather than an
        # exemption: an atomic step on the far side of a socket has no window
        # to park in, and the hammer below decides atomicity for it -- at the
        # false-pass rate stated above, which is then carrying the whole
        # property rather than backing up a gate.
        #
        # Declaring no seam is not a way out for a violator. `postgres-naive`
        # has none either, and is still expected to fail this property: it has
        # to fail the hammer instead, and if it stops doing so its strict xfail
        # turns that into a red run. Skipping here removes an instrument, never
        # the verdict.
        pytest.skip(
            f"{backend.name} has no in-process staging seam; atomicity is "
            "decided for it by the threaded probe"
        )

    store = stepped.store
    key = stepped.key()

    seed = store.compare_and_set(key, ABSENT, Record(b"seed", "seed"))
    assert isinstance(seed, Applied)

    outcomes: dict[str, object] = {}

    def writer(name: str) -> None:
        outcomes[name] = store.compare_and_set(
            key, seed.version, Record(name.encode(), name)
        )

    first = threading.Thread(target=writer, args=("a",), name="writer-a")
    second = threading.Thread(target=writer, args=("b",), name="writer-b")

    gate.park_thread(first)
    first.start()
    gate.wait_until_parked()

    # `b` runs to *completion* while `a` is parked, and this waits for it
    # rather than hoping. Giving `b` a fixed window and carrying on either way
    # would let a loaded machine produce a run where the two writes never
    # overlapped -- and a stepper that missed its own interleaving reports
    # green, which is the precise failure this file exists to argue against. The seam is outside every lock, so `b`
    # cannot be blocked by `a` and there is nothing left to race.
    second.start()
    second.join(timeout=30.0)
    assert not second.is_alive(), (
        "the second writer did not finish while the first was parked, so the "
        "interleaving under test never happened and this run decided nothing"
    )
    assert gate.arrivals_by_thread.get("writer-b", 0) >= 1, (
        "the second writer never reached the store's staging seam, so it was "
        f"never inside the store at all: {gate.arrivals_by_thread}"
    )

    gate.release()
    first.join(timeout=30.0)
    assert not first.is_alive(), "the parked writer never resumed"

    applied = {
        name: outcome
        for name, outcome in outcomes.items()
        if isinstance(outcome, Applied)
    }
    assert len(applied) == 1, (
        f"two writes decided from version {seed.version} both applied, so one "
        f"was overwritten by a caller that had not seen it: {outcomes}"
    )

    winner = next(iter(applied))
    final = store.read(key)
    assert final.record is not None
    # Payload rather than token: a store that substitutes tokens is wrong about
    # a different property, and this check must not report that one.
    assert final.record.payload == winner.encode(), (
        f"{winner} was told its write applied, and the store holds "
        f"{final.record.payload!r}"
    )
    assert final.version == applied[winner].version


def _increment_once(store: Store, key: str, tag: str) -> None:
    """Read, add one, compare-and-set; retry from the version a refusal reports.

    The same loop the hammer's workers run, lifted out so the stepped half below
    and the hammer increment the same way and differ only in who decides the
    interleaving: a gate there, the scheduler here.
    """
    for _ in range(MAX_RETRIES):
        reading = store.read(key)
        assert reading.record is not None
        nxt = str(int(reading.record.payload) + 1).encode()
        outcome = store.compare_and_set(key, reading.version, Record(nxt, tag))
        if isinstance(outcome, Applied):
            return
    raise AssertionError(f"{tag} never applied in {MAX_RETRIES} tries")


def _stepped_increment_loses_nothing(backend: BackendUnderTest) -> bool:
    """Two incrementers from one version, the interleaving decided by the seam.

    Returns False when the backend has no in-process seam, so the caller knows
    the hammer is carrying the property alone. On a store that has one: writer A
    is parked at `_staged`, writer B runs to completion and is *waited for*, then
    A is released. A conforming store refuses A's stale comparison and A retries
    from the version the refusal reports, so the counter reads two. The naive
    store decided A's comparison before the seam and writes on top of B after it,
    so the counter reads one -- a lost update, in one step, on every run rather
    than on the runs where the scheduler happened to land there.
    """
    gate = Gate()
    stepped = backend.stepped(gate)
    if stepped is None:
        return False
    store = stepped.store
    key = stepped.key()
    seed = store.compare_and_set(key, ABSENT, Record(b"0", "seed"))
    assert isinstance(seed, Applied)

    # Carried out of the threads and raised BEFORE the counter is read, as the
    # hammer and the two-writers seam test both do: an incrementer that gave up
    # or hit a bad reading would otherwise leave the counter short and report as
    # a LOST increment - a reading is not an attribution.
    errors: list[BaseException] = []

    def run(tag: str) -> None:
        try:
            _increment_once(store, key, tag)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread
            errors.append(exc)

    first = threading.Thread(target=run, args=("stepped-a",), name="stepped-a")
    second = threading.Thread(target=run, args=("stepped-b",), name="stepped-b")
    gate.park_thread(first)
    first.start()
    gate.wait_until_parked()
    second.start()
    second.join(timeout=30.0)
    assert not second.is_alive(), (
        "the second incrementer did not finish while the first was parked, so the "
        "interleaving under test never happened and this half decided nothing"
    )
    assert gate.arrivals_by_thread.get("stepped-b", 0) >= 1, (
        "the second incrementer never reached the store's staging seam: "
        f"{gate.arrivals_by_thread}"
    )
    gate.release()
    first.join(timeout=30.0)
    assert not first.is_alive(), "the parked incrementer never resumed"
    if errors:
        raise errors[0]

    final = store.read(key)
    assert final.record is not None
    observed = int(final.record.payload)
    assert observed == 2, (
        f"two incrementers from one version, one parked between its read and its "
        f"write: {2 - observed} increment was applied and then lost, and the writer "
        f"that lost it was told it had applied"
    )
    return True


@pytest.mark.conformance("atomicity")
def test_concurrent_increments_lose_nothing(backend: BackendUnderTest) -> None:
    # Decided through the seam first, where there is one. This half is
    # deterministic: on the naive store it fails on every run, which is what its
    # strict xfail needs to be a control rather than a coin flip. The hammer
    # below is the probe, and on a store with no seam it is the only
    # instrument, at the false-pass rate stated at the top of this file.
    _stepped_increment_loses_nothing(backend)

    store = backend.store
    key = backend.key()

    # Seeded before the threads start so the key is present throughout. A race
    # to *create* is a different question, decided by the compare-and-set
    # property, and mixing the two would let one failure report as the other.
    seed = store.compare_and_set(key, ABSENT, Record(b"0", "seed"))
    assert isinstance(seed, Applied)

    errors: list[BaseException] = []

    #: How many times a writer was refused because another had moved the version
    #: between its read and its write. That is the window itself: a refusal can
    #: only be produced by a genuine overlap, so a non-zero count is evidence the
    #: probe raced rather than an inference that it probably did.
    conflicts = 0
    conflicts_lock = threading.Lock()

    def worker(worker_id: int) -> None:
        nonlocal conflicts
        seen = 0
        try:
            for i in range(HAMMER_ITERATIONS):
                for _ in range(MAX_RETRIES):
                    reading = store.read(key)
                    assert reading.record is not None
                    nxt = str(int(reading.record.payload) + 1).encode()
                    outcome = store.compare_and_set(
                        key, reading.version, Record(nxt, f"w{worker_id}-{i}")
                    )
                    if isinstance(outcome, Applied):
                        break
                    seen += 1
                else:
                    raise AssertionError(
                        f"worker {worker_id} never applied in {MAX_RETRIES} tries"
                    )
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread
            errors.append(exc)
        finally:
            # In `finally` so a worker that raised still reports what it saw: the
            # count is evidence about the run, not about the outcome.
            with conflicts_lock:
                conflicts += seen

    threads = [
        threading.Thread(target=worker, args=(n,), name=f"hammer-{n}")
        for n in range(HAMMER_THREADS)
    ]
    with preemptive():
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60.0)
    assert not any(t.is_alive() for t in threads), "a hammer thread never finished"
    if errors:
        raise errors[0]

    final = store.read(key)
    assert final.record is not None
    expected = HAMMER_THREADS * HAMMER_ITERATIONS
    observed = int(final.record.payload)

    # The order of these two is the control, not a style choice.
    #
    # A lost increment is itself proof the window was reached -- two writers read
    # one version and both were told they had applied -- so it is decided first,
    # and decided as a finding. Consulting `conflicts` first would report a
    # caught violation as an inconclusive run, because a store that lets both
    # writers through produces NO refusals: on a violator the count is
    # legitimately zero.
    assert observed == expected, (
        f"{expected - observed} increments were applied and "
        f"then lost: every one of them was reported to a caller as having "
        f"happened"
    )

    # Nothing was lost. That is evidence the property holds only if the writers
    # actually collided, and on a loaded machine they may not have: the threads
    # can serialise so completely that every compare-and-set applies first try,
    # in which case this run exercised a counter rather than an atomicity
    # property.
    #
    # Without this skip, a missed race against a REAL backend would be
    # indistinguishable from a held property -- both green -- so a real store
    # passing (`postgres`, `duckdb`) would carry an unmeasured false-pass rate.
    # A miss against a VIOLATOR is already loud, via `XPASS(strict)`.
    if conflicts == 0:
        pytest.skip(
            f"the probe never raced: {HAMMER_THREADS} writers made {expected} "
            f"increments without one refusal, so no two of them ever held the "
            f"same version at once. This run says nothing about atomicity in "
            f"either direction -- it is not a pass, and on a violator it is not "
            f"an XPASS either"
        )



#: Rounds of the threaded creation race, used only where a backend has no seam.
#: Each round is a fresh key, because a creation can be raced exactly once per
#: key -- unlike the hammer above, which reuses one key for every iteration.
#:
#: Calibrated against `postgres-naive`, the only known-bad store that reaches
#: this path. The in-process violators do not reach it at all; they are decided
#: by the stepper, deterministically. That split is not symmetry -- see the
#: docstring below for why.
CREATION_ROUNDS = 40
CREATION_RACERS = 8


@pytest.mark.conformance("atomicity")
def test_two_creators_racing_from_absence_do_not_both_apply(
    backend: BackendUnderTest,
) -> None:
    """The race the tests above cannot reach: two callers CREATING one key at once.

    **The tests above race only updaters.** Both concurrent tests above seed
    their key single-threaded and then race from the version that seeding
    returned, so every race they run is between *updaters*. The `INSERT` branch
    of a real backend -- the path taken when `expected_version` is `ABSENT` --
    is entered by two callers at once only here. It is the path a lease's
    `acquire` takes under load, since it creates a key from absence.

    **Two halves, for the reason the rest of this file has two halves.** The
    stepper is the gate wherever there is a seam to park in, and it decides
    without reference to timing; the threaded race is a probe, and reaches only
    the backends the stepper cannot.

    **That split is forced rather than chosen for symmetry.** A creation can
    be raced only once per key, so each round is a single shot, and on a loaded
    machine the eight threads serialise and every shot can miss at once. A
    violator that gets away turns its strict xfail red, and one shot per round
    is not something a strict xfail can stand on.

    **Declared against `atomicity` rather than `compare-and-set`, and the
    routing was checked against the violators rather than argued:**

    - `naive` reads, then writes, so both creators pass a check that was true
      when they made it. Caught by the count.
    - `conflict-writes` reports the loser's conflict honestly and writes anyway,
      so exactly one `Applied` comes back and the value left behind belongs to a
      loser. Caught by the survivor check.
    - `create-always` is **expected to pass this, and does.** Its defect is that
      absence is not a version: it applies to an absent key whose version has
      moved past zero, which is deletion, not creation. A fresh key never
      exercises it. So a *concurrent* creation race lands on `atomicity`, not
      where `test_concurrent_increments_lose_nothing`'s comment routes it.
    """
    gate = Gate()
    stepped = backend.stepped(gate)
    if stepped is not None:
        _creation_race_stepped(stepped, gate)
        return
    _creation_race_threaded(backend)


def _creation_race_stepped(backend: BackendUnderTest, gate: Gate) -> None:
    """Two creators, interleaved deterministically. No timing, and no rate."""
    store = backend.store
    key = backend.key()
    outcomes: dict[str, object] = {}

    def creator(name: str) -> None:
        outcomes[name] = store.compare_and_set(key, ABSENT, Record(name.encode(), name))

    first = threading.Thread(target=creator, args=("a",), name="creator-a")
    second = threading.Thread(target=creator, args=("b",), name="creator-b")

    gate.park_thread(first)
    first.start()
    gate.wait_until_parked()

    # `b` runs to completion while `a` is parked, and is waited for rather than
    # given a window -- the seam sits outside every lock, so `a` blocks nobody.
    second.start()
    second.join(timeout=30.0)
    assert not second.is_alive(), (
        "the second creator did not finish while the first was parked, so the "
        "interleaving under test never happened and this run decided nothing"
    )
    assert gate.arrivals_by_thread.get("creator-b", 0) >= 1, (
        "the second creator never reached the store's staging seam, so it was "
        f"never inside the store at all: {gate.arrivals_by_thread}"
    )

    gate.release()
    first.join(timeout=30.0)
    assert not first.is_alive(), "the parked creator never resumed"

    applied = {n: o for n, o in outcomes.items() if isinstance(o, Applied)}
    assert len(applied) == 1, (
        "two creators both decided from absence and both applied, so one "
        f"creation was overwritten by a caller that had not seen it: {outcomes}"
    )

    winner = next(iter(applied))
    final = store.read(key)
    assert final.record is not None, "the key vanished after a successful create"
    # Payload rather than token, for the reason the stepped test above gives: a
    # store that substitutes tokens is wrong about a different property, and
    # this assertion must not report that one as an atomicity failure.
    assert final.record.payload == winner.encode(), (
        f"{winner} was told its creation applied, and the store holds "
        f"{final.record.payload!r}"
    )


def _creation_race_threaded(backend: BackendUnderTest) -> None:
    """The probe, for stores whose atomic step is not in this process."""
    store = backend.store
    multiple_applied: list[tuple[str, int]] = []
    losers_left_behind: list[str] = []

    for _round in range(CREATION_ROUNDS):
        key = backend.key()
        applied: dict[str, Applied] = {}
        errors: list[BaseException] = []
        start = threading.Barrier(CREATION_RACERS)

        def creator(name: str) -> None:
            try:
                start.wait(timeout=30.0)
                outcome = store.compare_and_set(
                    key, ABSENT, Record(name.encode(), name)
                )
                if isinstance(outcome, Applied):
                    applied[name] = outcome
            except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread
                errors.append(exc)

        threads = [
            threading.Thread(
                target=creator, args=(f"creator-{n}",), name=f"creator-{n}"
            )
            for n in range(CREATION_RACERS)
        ]
        with preemptive():
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60.0)
        assert not any(t.is_alive() for t in threads), "a creator thread never finished"
        if errors:
            # An exception is a failure, not a conflict. A backend whose lost
            # creation raises instead of returning `Conflict` has not reported an
            # outcome at all.
            raise errors[0]

        if len(applied) != 1:
            multiple_applied.append((key, len(applied)))
            continue

        winner = next(iter(applied))
        reading = store.read(key)
        assert reading.record is not None, "the key vanished after a successful create"
        if reading.record.payload != winner.encode():
            losers_left_behind.append(
                f"{key}: {winner} was told it applied, "
                f"{reading.record.payload!r} is stored"
            )

    assert not multiple_applied, (
        f"{len(multiple_applied)} of {CREATION_ROUNDS} rounds had more than one "
        "creator told its write applied, so absence was compared and written in "
        f"two steps: {multiple_applied[:3]}"
    )
    assert not losers_left_behind, (
        f"{len(losers_left_behind)} of {CREATION_ROUNDS} rounds left a loser's "
        "value in the store behind a conflict it reported honestly: "
        f"{losers_left_behind[:3]}"
    )
