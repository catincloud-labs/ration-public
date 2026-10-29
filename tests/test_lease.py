"""The lease service, against every store that is not a violator.

These are **not** conformance tests and deliberately do not use the `backend`
fixture. Conformance decides whether a store implements the contract; this file
decides whether a lease built on the contract behaves, and running it against
implementations that are wrong on purpose would only re-report findings the
suite already owns. What it runs against is every case declaring no violations
-- `correct`, and `postgres` whenever a server is configured.

**One negative control does live here**, at the bottom: the racing-acquire
check is re-run against `naive`, which must double-grant. A concurrency check
that has never been observed to fail has not been shown to be a check, and this
one is otherwise asserting the absence of something.
"""

from __future__ import annotations

import threading

import pytest

from ration.contract import Store, Unreachable
from ration.lease import Lease, acquire, release, renew
from tests.support.backends import CASES, REAL_TTL, BackendUnderTest
from tests.support.gate import Gate
from tests.support.preemption import preemptive
from tests.violators import NaiveStore

#: Every implementation expected to be correct. Filtered rather than listed, so
#: a store added to the registry is exercised here without this file changing.
CORRECT_CASES = [case for case in CASES if not case.violates]

#: Claimants contending for one key, and attempts each. Real threads against a
#: real store, and for `postgres` the only instrument there is -- an atomic step
#: on the far side of a socket has no seam to park in.
#:
#: **These counts are not what the exclusivity property rests on.** The
#: negative control at the bottom of this file is a *gate*, and it is
#: deliberately not built from these: this probe catches `naive` in some runs
#: and misses it in others, so a run of it that finds nothing is weak evidence
#: rather than a verdict.
CLAIMANTS = 8
ATTEMPTS = 50


@pytest.fixture(params=CORRECT_CASES, ids=[case.name for case in CORRECT_CASES])
def correct(request: pytest.FixtureRequest) -> BackendUnderTest:
    case = request.param
    return case.build()


# -- exclusivity -----------------------------------------------------------


def test_a_grant_is_exclusive(correct: BackendUnderTest) -> None:
    key = correct.key()
    held = acquire(correct.store, key, "worker-a")
    assert held is not None
    assert acquire(correct.store, key, "worker-b") is None, (
        "a second holder was granted a key that was already taken, which is the "
        "whole of what a lease is for"
    )


def test_a_holder_reacquiring_its_own_grant_is_told_it_holds_it(
    correct: BackendUnderTest,
) -> None:
    """A caller that reuses its identity is asking whether it holds the key."""
    key = correct.key()
    first = acquire(correct.store, key, "worker-a", payload=b"pid-1")
    assert first is not None
    again = acquire(correct.store, key, "worker-a")
    assert again is not None
    assert again.version == first.version, "re-acquiring must not write"
    assert again.payload == b"pid-1", "the grant already in the store is the one"


def test_a_released_grant_is_free(correct: BackendUnderTest) -> None:
    key = correct.key()
    held = acquire(correct.store, key, "worker-a")
    assert held is not None
    assert release(correct.store, held) is True
    assert acquire(correct.store, key, "worker-b") is not None


def test_an_expired_grant_is_free(correct: BackendUnderTest) -> None:
    key = correct.key()
    held = acquire(correct.store, key, "worker-a", ttl=REAL_TTL)
    assert held is not None
    assert acquire(correct.store, key, "worker-b", ttl=REAL_TTL) is None

    correct.advance_store_time(REAL_TTL * 2)

    assert acquire(correct.store, key, "worker-b", ttl=REAL_TTL) is not None, (
        "the grant's TTL passed and the key stayed held, so expiry is not "
        "reaching the lease"
    )


# -- the fencing token -----------------------------------------------------


def test_renewing_moves_the_grant_forward_and_spends_the_old_one(
    correct: BackendUnderTest,
) -> None:
    key = correct.key()
    first = acquire(correct.store, key, "worker-a", ttl=REAL_TTL)
    assert first is not None

    second = renew(correct.store, first)
    assert second is not None
    assert second.version > first.version

    # The fencing property, with no timing in it. The lease this caller was
    # holding a moment ago is not merely old, it is spent -- and it is spent
    # because the version moved, which is the only thing the store promises.
    assert renew(correct.store, first) is None, (
        "a superseded grant renewed itself, so the version is not fencing "
        "anything and a stalled holder could write after being replaced"
    )


def test_a_holder_that_lost_the_key_cannot_renew(
    correct: BackendUnderTest,
) -> None:
    key = correct.key()
    first = acquire(correct.store, key, "worker-a", ttl=REAL_TTL)
    assert first is not None

    correct.advance_store_time(REAL_TTL * 2)
    successor = acquire(correct.store, key, "worker-b", ttl=REAL_TTL)
    assert successor is not None

    assert renew(correct.store, first) is None, (
        "the previous holder extended a grant that had already been given to "
        "somebody else, which is the lease held twice"
    )


def test_an_expired_grant_nobody_took_can_still_be_renewed(
    correct: BackendUnderTest,
) -> None:
    """Documented behaviour, asserted so it cannot drift into a surprise.

    Expiry does not advance a version, so a holder whose TTL passed while nobody
    else acquired renews successfully. That is safe rather than lenient: the
    version being unmoved is proof that no second grant was ever made, which is
    the only thing exclusivity requires.
    """
    key = correct.key()
    held = acquire(correct.store, key, "worker-a", ttl=REAL_TTL)
    assert held is not None

    correct.advance_store_time(REAL_TTL * 2)

    assert renew(correct.store, held) is not None


def test_releasing_a_lost_grant_does_not_disturb_the_new_holder(
    correct: BackendUnderTest,
) -> None:
    """The destructive case, and the reason `release` is a compare-and-set.

    A holder that has been replaced still has a `Lease` object and may still run
    its cleanup. If `release` deleted by key, that cleanup would revoke the
    successor's grant -- a caller that lost its lease reaching in and taking the
    lease away from whoever holds it now.
    """
    key = correct.key()
    first = acquire(correct.store, key, "worker-a", ttl=REAL_TTL)
    assert first is not None

    correct.advance_store_time(REAL_TTL * 2)
    successor = acquire(correct.store, key, "worker-b", ttl=REAL_TTL)
    assert successor is not None

    assert release(correct.store, first) is False

    still_held = correct.store.read(key)
    assert still_held.record is not None, "the successor's grant was deleted"
    assert still_held.record.token == "worker-b"


# -- what the substrate refuses --------------------------------------------


def test_an_unreachable_store_grants_nothing(correct: BackendUnderTest) -> None:
    """Property 3, reaching the consumer rather than stopping at the store."""
    key = correct.key()
    with correct.partition():
        with pytest.raises(Unreachable):
            acquire(correct.store, key, "worker-a", ttl=REAL_TTL)

    # And nothing was left behind by the attempt.
    assert correct.store.read(key).record is None


# -- property 4, from the first caller that actually needs it --------------


def test_an_ambiguous_grant_that_landed_is_recovered(
    correct: BackendUnderTest,
) -> None:
    key = correct.key()
    with correct.ambiguity(applied=True):
        granted = acquire(correct.store, key, "worker-a", ttl=REAL_TTL)

    assert granted is not None, (
        "the grant is in the store and the caller was told it failed, so a "
        "holder that does hold the lease will not act on it"
    )
    held = correct.store.read(key)
    assert held.record is not None and held.record.token == "worker-a"
    assert granted.version == held.version, (
        "the recovered grant carries the wrong version, so its next renew or "
        "release compares against a fence that was never there"
    )


def test_an_ambiguous_grant_that_never_landed_is_not_claimed(
    correct: BackendUnderTest,
) -> None:
    key = correct.key()
    with correct.ambiguity(applied=False):
        granted = acquire(correct.store, key, "worker-a", ttl=REAL_TTL)

    assert granted is None
    assert correct.store.read(key).record is None


def test_an_ambiguous_renew_that_never_landed_is_not_claimed(
    correct: BackendUnderTest,
) -> None:
    """The regression this consumer exists to catch.

    A `resolve` that tested the caller's token before the key's version would
    resolve a renew whose answer was lost as `APPLIED`, on the evidence of the
    *previous* grant's record -- the caller's own token, already sitting in the
    store. The holder would be handed a lease it had not extended and believe
    it had a fresh TTL.

    The token only stops identifying a *write* once a caller writes the same
    key twice. The conformance suite reaches that case at the store, in
    `test_ambiguity.py`'s `test_a_rewriters_lost_answer_is_resolved`; this test
    holds it at the lease, where a renew is the routine rewrite.
    """
    key = correct.key()
    first = acquire(correct.store, key, "worker-a", ttl=REAL_TTL)
    assert first is not None

    with correct.ambiguity(applied=False):
        extended = renew(correct.store, first)

    assert extended is None, (
        "a renew that never reached the store was reported as applied, so the "
        "holder is running on a TTL the store does not know about"
    )
    assert correct.store.read(key).version == first.version, (
        "the store moved, so this test is not exercising the case it claims"
    )


def test_an_ambiguous_renew_that_landed_is_recovered(
    correct: BackendUnderTest,
) -> None:
    key = correct.key()
    first = acquire(correct.store, key, "worker-a", ttl=REAL_TTL)
    assert first is not None

    with correct.ambiguity(applied=True):
        extended = renew(correct.store, first)

    assert extended is not None
    assert extended.version > first.version
    assert extended.version == correct.store.read(key).version


def test_an_ambiguous_release_gives_up_rather_than_guessing(
    correct: BackendUnderTest,
) -> None:
    """The half of property 4 that a deletion does not reach.

    A delete removes the record carrying the caller's token, so an applied
    release and a key taken by somebody else read identically. `release` claims
    the weaker of the two, and the TTL is what makes that safe.
    """
    key = correct.key()
    held = acquire(correct.store, key, "worker-a", ttl=REAL_TTL)
    assert held is not None

    with correct.ambiguity(applied=True):
        ended = release(correct.store, held)

    assert ended is False, "an unresolvable release must not claim to have ended"
    # It did in fact apply. The point is that the caller was not told so, and
    # that what it was told is safe: the key is free either way.
    assert correct.store.read(key).record is None


# -- concurrency, and proof this check can fail ----------------------------


def _exclusivity_violations(store: Store, key: str) -> int:
    """Contend for one key, and count the grants that were not exclusive.

    Each claimant acquires, and then **reads the key it was just told it
    holds.** Under a correct store that read can only return its own record: no
    other compare-and-set from a stale version can land while this grant stands.
    A record carrying somebody else's token means a second holder was granted a
    key this claimant already held, and the count rises.

    **The assertion is cumulative**, not a question of whether two holders can
    be seen *at one instant* -- the negative control at the bottom of this file
    says why that shape fails. A violation is counted **per operation**, over
    `CLAIMANTS * ATTEMPTS` of them, so a single lost grant anywhere in the run
    fails it. That is the shape `test_concurrent_increments_lose_nothing`
    already uses: it asserts on a total at the end rather than on a moment.

    **It remains a probe, and it is not what the property rests on.** A run
    that finds nothing has not shown there was nothing to find. What the
    property rests on is the gate below -- except for `postgres`, whose atomic
    step is on the far side of a socket, where this is the only instrument
    there is.
    """
    guard = threading.Lock()
    violations = 0
    errors: list[BaseException] = []

    def claimant(number: int) -> None:
        nonlocal violations
        try:
            for attempt in range(ATTEMPTS):
                lease = acquire(store, key, f"claimant-{number}-{attempt}")
                if lease is None:
                    continue
                held = store.read(key)
                if held.record is None or held.record.token != lease.holder:
                    with guard:
                        violations += 1
                release(store, lease)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread
            with guard:
                errors.append(exc)

    threads = [
        threading.Thread(target=claimant, args=(n,), name=f"claimant-{n}")
        for n in range(CLAIMANTS)
    ]
    with preemptive():
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120.0)

    assert not any(t.is_alive() for t in threads), "a claimant never finished"
    if errors:
        raise errors[0]
    return violations


def test_a_grant_is_never_held_twice_under_load(
    correct: BackendUnderTest,
) -> None:
    """The lost update, at the lease.

    The contract exists to prevent a lost update: two concurrent callers
    snapshotting one ledger total, both admitted, the ceiling exceeded. The same
    shape at a lease is two holders each told the key is theirs alone, and it is
    the single thing a lease exists to prevent.
    """
    violations = _exclusivity_violations(correct.store, correct.key())
    assert violations == 0, (
        f"{violations} grant(s) were made on a key somebody already held, and "
        f"every holder involved had been told the lease was exclusively theirs"
    )


def test_a_non_atomic_store_grants_a_key_that_is_already_held() -> None:
    """The negative control, and it is a **gate rather than a probe.**

    `naive` reads and then writes with nothing joining the two, so two claimants
    can each be told they took the key. This parks one of them inside that gap,
    runs the other to completion, and then lets the first finish -- so the
    interleaving is arranged rather than waited for, and the test asserts that
    it actually happened.

    **Why not a probabilistic shape.** Three suggest themselves, and the first
    two cannot be relied on:

    1. **Threads started in a loop, counting grants.** Not a race at all -- an
       in-memory acquire finishes in microseconds, so the first claimant is
       done before the last has been started. `naive` grants the key once per
       round, every round, and the exclusivity check stays green while never
       reaching a window.
    2. **A `threading.Barrier`, then sampling a peak-holders counter.** A
       barrier synchronises *arrival*, and arrival is not overlap: on a small
       machine the winner can finish its whole compare-and-set before the loser
       is scheduled, and sampling then has to catch the overlap in the instant
       it exists. It can catch `naive` on one machine and miss it on another.
    3. **Counting a violation per operation**, the cumulative shape
       `test_concurrent_increments_lose_nothing` uses. This one **works**, in
       the sense that run against `naive` it catches it in some rounds and not
       in others.

    So shape 3 is the load probe above, and not the control. **Where a store
    has a seam, a gate is worth more than any per-round rate**: it reaches fewer
    places, and what it reaches it can prove it reached. A probe that catches a
    known-bad store some of the time is evidence about the probe; this test's
    parked thread is a verdict. The probe above keeps its place because
    `postgres` has no seam to park in, and there it is the only instrument
    there is.

    **Shown to go quiet as well as to fire**, because a control observed only
    to fire has not been shown to discriminate. This test has no quiet arm of
    its own: a pool of one hands its grant to `acquire` here, and
    `test_pool.py`'s `test_a_non_atomic_store_grants_one_slot_to_two_holders`
    runs this interleaving through it against `MemoryStore`, which grants
    **one** holder.
    """
    gate = Gate()
    # Parked at `naive`'s own seam, which sits exactly in its gap: after the
    # read that decides the comparison, before the write that acts on it.
    # Subclassed rather than given a hook, so no store grows a constructor
    # parameter that exists only for a test.
    store = type(
        "ParkingNaiveStore", (NaiveStore,), {"_staged": lambda self: gate.arrive()}
    )()
    key = "control/deterministic"
    granted: dict[str, Lease | None] = {}

    def claim(name: str) -> None:
        granted[name] = acquire(store, key, name)

    first = threading.Thread(target=claim, args=("holder-a",), name="holder-a")
    second = threading.Thread(target=claim, args=("holder-b",), name="holder-b")

    gate.park_thread(first)
    first.start()
    gate.wait_until_parked()

    # `holder-b` runs to completion while `holder-a` is parked mid-write, and
    # this waits for it rather than hoping. Nothing can block it: the seam is
    # outside every lock, so a parked claimant holds nothing.
    second.start()
    second.join(timeout=30.0)
    assert not second.is_alive(), (
        "the second claimant did not finish while the first was parked, so the "
        "interleaving under test never happened and this run decided nothing"
    )
    assert gate.arrivals_by_thread.get("holder-b", 0) >= 1, (
        f"the second claimant never reached the store's seam, so it was never "
        f"inside the store at all: {gate.arrivals_by_thread}"
    )

    gate.release()
    first.join(timeout=30.0)
    assert not first.is_alive(), "the parked claimant never resumed"

    holders = [name for name, lease in granted.items() if lease is not None]
    assert len(holders) == 2, (
        f"a store that reads then writes granted the key to {len(holders)} "
        f"claimant(s), so the window this control exists to open was not opened "
        f"and the check above is not being shown to catch anything: {granted}"
    )

    # And this is precisely what `_exclusivity_violations` counts: two callers
    # each told the key is theirs, and one record in the store.
    held = store.read(key)
    assert held.record is not None
    outvoted = [name for name in holders if name != held.record.token]
    assert len(outvoted) == 1, (
        "both grants somehow survived, which a single-key store cannot do"
    )
