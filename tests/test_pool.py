"""The pool, against every store that is not a violator.

Same standing as `test_lease.py` and for the same reason: conformance decides
whether a store implements the contract, and this file decides whether a pool
built on the contract behaves. Running it against implementations that are wrong
on purpose would re-report findings the suite already owns.

**Two negative controls live here**, and they test different things. The
deterministic gate at the bottom shows that a pool over a non-atomic store hands
one slot to two holders. The `hash()` test above it is a control on this
module's own rotation, and it is the only test here that would still pass if the
pool were deleted.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

import pytest

from ration.contract import Unreachable
from ration.lease import Lease, acquire as acquire_slot, release, renew
from ration.memory import MemoryStore
from ration.pool import Pool, _order, acquire, holders
from tests.support.backends import CASES, REAL_TTL, BackendUnderTest
from tests.support.gate import Gate
from tests.support.preemption import preemptive
from tests.violators import NaiveStore

#: Filtered rather than listed, so a store added to the registry is exercised
#: here without this file changing.
CORRECT_CASES = [case for case in CASES if not case.violates]

#: Claimants and attempts for the load probe. Same caveat as the lease's:
#: these counts are not what the capacity property rests on. The gate at the
#: bottom is the verdict; this is the only instrument `postgres` admits.
CLAIMANTS = 8
ATTEMPTS = 25

#: Deliberately smaller than CLAIMANTS, so the pool is contended rather than
#: merely used. A pool nobody has to queue for proves nothing about refusal.
CAPACITY = 3


@pytest.fixture(params=CORRECT_CASES, ids=[case.name for case in CORRECT_CASES])
def correct(request: pytest.FixtureRequest) -> BackendUnderTest:
    case = request.param
    return case.build()


def _pool(backend: BackendUnderTest, capacity: int = CAPACITY) -> Pool:
    """A pool whose slot keys cannot collide with another test's.

    `key()` already hands out a unique name per call; the slots hang off it.
    """
    return Pool(backend.key(), capacity)


# -- capacity --------------------------------------------------------------


def test_a_pool_grants_exactly_its_capacity(correct: BackendUnderTest) -> None:
    pool = _pool(correct)
    granted = [
        acquire(correct.store, pool, f"worker-{n}") for n in range(CAPACITY)
    ]
    assert all(lease is not None for lease in granted), (
        f"a pool of {CAPACITY} refused a claimant while a slot was free: {granted}"
    )
    assert len({lease.key for lease in granted if lease}) == CAPACITY, (
        "two claimants were handed the same slot, which is the property the "
        "pool exists to hold"
    )
    assert acquire(correct.store, pool, "worker-overflow") is None, (
        "a pool granted beyond its capacity, which is overdraw and is the one "
        "thing the name refuses"
    )


def test_a_released_slot_is_granted_again(correct: BackendUnderTest) -> None:
    pool = _pool(correct)
    held = [acquire(correct.store, pool, f"worker-{n}") for n in range(CAPACITY)]
    assert acquire(correct.store, pool, "late") is None

    first = held[0]
    assert first is not None
    assert release(correct.store, first) is True

    recovered = acquire(correct.store, pool, "late")
    assert recovered is not None, "a freed slot was not granted to a waiting claimant"
    assert recovered.key == first.key


def test_an_expired_slot_is_granted_again(correct: BackendUnderTest) -> None:
    pool = _pool(correct, capacity=1)
    held = acquire(correct.store, pool, "worker-a", ttl=REAL_TTL)
    assert held is not None
    assert acquire(correct.store, pool, "worker-b") is None

    correct.advance_store_time(REAL_TTL * 2)

    assert acquire(correct.store, pool, "worker-b") is not None, (
        "an expired grant kept its slot, so the store is not expiring keys the "
        "pool depends on it to expire"
    )


# -- the finding: a retry must not consume a second slot --------------------


def test_a_holder_retrying_is_handed_the_slot_it_already_has(
    correct: BackendUnderTest,
) -> None:
    """The property this module was written around.

    A holder whose answer was lost retries. If the pool takes the first free
    slot it sees, that holder now occupies two -- and has a handle for only the
    second, so the first can be released by nothing but expiry. The pass has to
    find the existing grant before it takes anything.
    """
    pool = _pool(correct)
    first = acquire(correct.store, pool, "worker-a")
    assert first is not None

    again = acquire(correct.store, pool, "worker-a")
    assert again is not None
    assert again.key == first.key, (
        f"a retrying holder was handed a different slot ({again.key}) while "
        f"still holding {first.key}, so it now occupies two and can release one"
    )

    occupied = holders(correct.store, pool)
    assert list(occupied) == [first.key], (
        f"a retry consumed a second slot: {occupied}"
    )


def test_a_retry_finds_its_grant_behind_a_free_slot(
    correct: BackendUnderTest,
) -> None:
    """The pass has to be a full one, and this is the only shape that shows it.

    **The obvious version of this test cannot fail.** A holder's own slot is
    normally the *first* in its own rotation, because being first is how it got
    there -- so a scan that stops at the first free slot still meets the
    holder's grant before any free one, and passes.

    So the grant has to sit **behind a free slot in this holder's own rotation**,
    which does not happen by accident and is built here on purpose: occupy the
    holder's rotation start with somebody else, let the holder land further
    along, then free the slot in front of it.
    """
    holder = "worker-behind"
    pool = _pool(correct, capacity=4)
    order = [index % pool.capacity for index in _order(pool, holder)]

    # Somebody else on this holder's first choice, so the holder lands second.
    # Placed with the lease directly rather than through the pool: the blocker
    # has its own rotation, and hoping two rotations line up would make this
    # test's setup depend on the digest it is not testing.
    parked = acquire_slot(correct.store, pool.slot(order[0]), "blocker")
    assert parked is not None

    mine = acquire(correct.store, pool, holder)
    assert mine is not None
    assert mine.key == pool.slot(order[1]), (
        "the holder did not land where this test needs it to; the rotation or "
        "the scan order has changed and this test is no longer testing anything"
    )

    # Now free the slot in FRONT of the holder's grant, in the holder's order.
    assert release(correct.store, parked) is True

    again = acquire(correct.store, pool, holder)
    assert again is not None
    assert again.key == mine.key, (
        f"a free slot earlier in the rotation ({pool.slot(order[0])}) was taken "
        f"in preference to this holder's existing grant ({mine.key}), so the "
        f"holder now occupies two slots and has a handle for only one"
    )
    assert list(holders(correct.store, pool)) == [mine.key], (
        f"the retry consumed a second slot: {holders(correct.store, pool)}"
    )


# -- the pool adds no operations -------------------------------------------


def test_a_pool_grant_renews_and_releases_as_a_lease(
    correct: BackendUnderTest,
) -> None:
    """The module's central claim, asserted rather than stated in prose.

    A pool grant *is* a lease, so `lease.renew` and `lease.release` work on it
    unchanged. If this ever needed a pool-specific renew, the argument in
    `lease.py` -- that a pool is N keys, each one of these leases -- would have
    been wrong.
    """
    pool = _pool(correct, capacity=1)
    held = acquire(correct.store, pool, "worker-a", ttl=REAL_TTL)
    assert held is not None

    extended = renew(correct.store, held, ttl=REAL_TTL)
    assert extended is not None, "a pool grant could not be renewed as a lease"
    assert extended.version > held.version

    assert release(correct.store, extended) is True
    assert acquire(correct.store, pool, "worker-b") is not None


def test_an_unreachable_store_grants_no_slot(correct: BackendUnderTest) -> None:
    """A store that is gone must not read as a pool that is full.

    Both return "you have no slot" to a careless caller, and they mean opposite
    things: one is a pool doing its job and the other is the substrate being
    absent. Property 3 makes the second an exception so it cannot be mistaken
    for the first.
    """
    pool = _pool(correct)
    with correct.partition():
        with pytest.raises(Unreachable):
            acquire(correct.store, pool, "worker-a")


# -- a control on this module's own rotation --------------------------------


def test_the_rotation_does_not_depend_on_the_interpreter() -> None:
    """The `hash()` trap, asserted rather than trusted to a comment.

    Python salts string hashing per process. A rotation built on `hash()` would
    put the same holder at a different start in every process sharing the pool,
    and across restarts of the same one. **Nothing would break** -- exclusivity
    is the store's, not the rotation's -- which is exactly what would let it
    survive review and then make a retry scan in a different order than the
    grant did.

    This runs the rotation in two subprocesses under different explicit hash
    seeds. It fails on `hash()` and passes on a stable digest.
    """
    program = (
        "from ration.pool import Pool, _order;"
        "print(list(_order(Pool('p', 7), 'worker-a')))"
    )
    seeds = ["0", "12345"]
    outputs = []
    for seed in seeds:
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        outputs.append(result.stdout.strip())

    assert outputs[0] == outputs[1], (
        f"the slot rotation changed with PYTHONHASHSEED ({seeds[0]} gave "
        f"{outputs[0]}, {seeds[1]} gave {outputs[1]}), so it depends on the "
        f"interpreter rather than on the holder"
    )
    assert outputs[0], "the rotation produced nothing, so this compared two blanks"


# -- under load -------------------------------------------------------------


def _slot_violations(store, pool: Pool) -> int:
    """Grants whose slot did not actually hold the grantee's token."""
    violations = 0
    errors: list[BaseException] = []
    guard = threading.Lock()

    def claimant(number: int) -> None:
        nonlocal violations
        try:
            for attempt in range(ATTEMPTS):
                lease = acquire(store, pool, f"claimant-{number}-{attempt}")
                if lease is None:
                    continue
                held = store.read(lease.key)
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


def test_a_slot_is_never_held_twice_under_load(correct: BackendUnderTest) -> None:
    """More claimants than slots, all churning. The probe, not the verdict."""
    pool = _pool(correct)
    violations = _slot_violations(correct.store, pool)
    assert violations == 0, (
        f"{violations} grant(s) were made on a slot somebody already held, and "
        f"every holder involved had been told the slot was exclusively theirs"
    )


def test_a_non_atomic_store_grants_one_slot_to_two_holders() -> None:
    """The negative control, and a **gate rather than a probe.**

    Capacity one, so the pool's promise is at its starkest: exactly one grant.
    `naive` reads and then writes with nothing joining the two, and this parks
    one claimant inside that gap, runs the other to completion, then lets the
    first finish. The interleaving is arranged rather than waited for, and the
    test asserts it actually happened.

    **Proven to go quiet as well as to fire**, because a control observed only
    to fire has not been shown to discriminate: the sibling assertion at the
    end runs the identical interleaving against a correct store, which grants
    one holder.
    """
    gate = Gate()
    store = type(
        "ParkingNaiveStore", (NaiveStore,), {"_staged": lambda self: gate.arrive()}
    )()
    pool = Pool("control/pool", 1)
    granted: dict[str, Lease | None] = {}

    def claim(name: str) -> None:
        granted[name] = acquire(store, pool, name)

    first = threading.Thread(target=claim, args=("holder-a",), name="holder-a")
    second = threading.Thread(target=claim, args=("holder-b",), name="holder-b")

    gate.park_thread(first)
    first.start()
    gate.wait_until_parked()

    second.start()
    second.join(timeout=30.0)
    assert not second.is_alive(), (
        "the second claimant did not finish while the first was parked, so the "
        "interleaving under test never happened and this run decided nothing"
    )
    assert gate.arrivals_by_thread.get("holder-b", 0) >= 1, (
        f"the second claimant never reached the store's seam: "
        f"{gate.arrivals_by_thread}"
    )

    gate.release()
    first.join(timeout=30.0)
    assert not first.is_alive(), "the parked claimant never resumed"

    winners = [name for name, lease in granted.items() if lease is not None]
    assert len(winners) == 2, (
        f"a pool of one over a non-atomic store granted {len(winners)} "
        f"claimant(s), so the window this control exists to open was not "
        f"opened and the capacity check above is not shown to catch "
        f"anything: {granted}"
    )
    assert len(holders(store, pool)) == 1, (
        "both grants somehow survived in the store, which one slot cannot do"
    )

    # And the same interleaving against a correct store: the parked claimant
    # resumes into a conflict, not a second grant.
    quiet_gate = Gate()
    correct_store = type(
        "ParkingMemoryStore",
        (MemoryStore,),
        {"_staged": lambda self: quiet_gate.arrive()},
    )()
    granted.clear()

    def claim_correct(name: str) -> None:
        granted[name] = acquire(correct_store, pool, name)

    first = threading.Thread(target=claim_correct, args=("holder-a",), name="holder-a")
    second = threading.Thread(target=claim_correct, args=("holder-b",), name="holder-b")
    quiet_gate.park_thread(first)
    first.start()
    quiet_gate.wait_until_parked()
    second.start()
    second.join(timeout=30.0)
    assert not second.is_alive()
    assert quiet_gate.arrivals_by_thread.get("holder-b", 0) >= 1, (
        f"the second claimant never reached the correct store's seam: "
        f"{quiet_gate.arrivals_by_thread}"
    )
    quiet_gate.release()
    first.join(timeout=30.0)
    assert not first.is_alive()

    winners = [name for name, lease in granted.items() if lease is not None]
    assert winners == ["holder-b"], (
        f"the identical interleaving against a correct store granted "
        f"{winners}, so the control above is failing every comer rather "
        f"than discriminating: {granted}"
    )
