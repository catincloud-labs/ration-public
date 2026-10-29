"""Property 2: expiry is evaluated by the store, never by a caller.

A backend may satisfy this natively or by evaluating a clock predicate at write
time: the suite decides only what is observable, and refuses a backend that
trusts a timestamp a client computed. That has a cost: it rules out any store
that cannot express a
predicate over its own clock, chosen deliberately, because the alternative is
comparing wall clocks across hosts and clock skew is a defect that presents as
a lease held twice. Violated by `client-expiry` and by `stale-clock`.

Two ways to get this wrong, and they need different instruments:

- **Using the caller's clock.** Caught by skew. A store that never consults a
  caller's clock has nothing to skew, and the assertion then holds trivially --
  which is the correct result for it rather than a weakened test.
- **Using the store's clock, somewhere else.** Skew cannot reach this one: the
  reading is the store's own and survives any amount of it. What gives it away
  is a delay between fetching the clock and deciding, which is a stepper.

Both live in one test rather than two. A store expected to fail this property
is marked strict-xfail against every test carrying it, and each of these two
violators is correct about the other's mistake -- so as separate tests, each
would pass one and turn its own expected failure into a red run.

The ttl is a fraction of a second because `advance_store_time` is a **real
sleep** for a real backend. A fake clock skips a minute for free; `now()` on a
server cannot be moved at all, which is the constraint a non-fake backend
imposes on this file.
"""

from __future__ import annotations

import threading

import pytest

from ration.contract import ABSENT, Applied, Reading, Record
from tests.support.backends import REAL_TTL, BackendUnderTest
from tests.support.gate import Gate


@pytest.mark.conformance("store-evaluated-expiry")
def test_expiry_is_decided_by_the_store_when_it_is_asked(
    backend: BackendUnderTest,
) -> None:
    store = backend.store
    key = backend.key()

    granted = store.compare_and_set(
        key, ABSENT, Record(b"holder-a", "caller-a"), ttl=REAL_TTL
    )
    assert isinstance(granted, Applied)
    assert store.read(key).present

    # The caller's clock is an hour fast. On one host this is invisible; across
    # two it is ordinary skew, and here it must change nothing at all.
    backend.skew_client_clock(3_600.0)
    still_held = store.read(key)
    assert still_held.present, (
        "a lease expired because a caller's clock said so, which is the same "
        "lease being free for one holder and held for another"
    )
    assert still_held.record is not None
    assert still_held.record.payload == b"holder-a"

    # And expiry happens at all. Without this, a store that simply ignores
    # `ttl` passes the assertion above for entirely the wrong reason.
    backend.advance_store_time(REAL_TTL * 2)
    assert not store.read(key).present, "the ttl was never evaluated by anyone"

    # Expiring is not a write: the key's version is where the last write left
    # it, so a holder's fencing token is not invalidated by time passing.
    assert store.read(key).version == granted.version

    # And the absence of a ttl is a decision rather than a default of zero.
    permanent = backend.key()
    forever = store.compare_and_set(permanent, ABSENT, Record(b"forever", "caller-b"))
    assert isinstance(forever, Applied)
    backend.advance_store_time(REAL_TTL * 2)
    backend.skew_client_clock(86_400.0)
    assert store.read(permanent).present

    _the_decision_is_not_taken_early(backend)


def _the_decision_is_not_taken_early(backend: BackendUnderTest) -> None:
    """Park a read between fetching the clock and deciding, then let time pass."""
    gate = Gate()
    stepped = backend.stepped(gate)
    if stepped is None:
        # No in-process seam. A store that decides inside one server-side
        # statement has no gap between fetching its clock and using it, which
        # is the point rather than a hole.
        #
        # Unlike the stepper in `test_atomicity.py` this needs no guard against
        # a violator declining a seam: both violators of this property are
        # in-process and both expose one, and a third that did not would still
        # have to survive the skew block above.
        return

    store = stepped.store
    key = stepped.key()

    granted = store.compare_and_set(
        key, ABSENT, Record(b"holder-a", "caller-a"), ttl=REAL_TTL
    )
    assert isinstance(granted, Applied)

    observed: dict[str, Reading] = {}

    def reader() -> None:
        observed["reading"] = store.read(key)

    thread = threading.Thread(target=reader, name="expiry-reader")
    gate.park_thread(thread)
    thread.start()

    # A store with no window on its read path simply finishes, and there is
    # nothing here to catch it with. That is not a hole the suite is hiding:
    # `stale-clock` is a registered violator of this property, so if it ever
    # stops parking, its expected failure becomes a pass and the run goes red.
    parked = gate.wait_until_parked_or_finished(thread)

    # The lease expires while the decision is in flight.
    stepped.advance_store_time(REAL_TTL * 2)
    gate.release()
    thread.join(timeout=30.0)
    assert not thread.is_alive(), "the reader never finished"

    if parked:
        assert not observed["reading"].present, (
            "the lease was reported as held on the strength of a clock reading "
            "taken before it expired, so expiry was settled at fetch time by "
            "whoever held that reading rather than by the store when asked"
        )

    assert not store.read(key).present
