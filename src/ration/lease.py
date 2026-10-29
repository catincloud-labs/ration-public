"""The lease service: a bounded claim on a key, granted for a period.

This is a **consumer** of the contract. `contract.py` states the substrate and
the suite decides whether a backend implements it; this module calls the
contract the way any user of a store would, rather than to judge it.

That matters more than the code does. The contract is falsified if a real
consumer needs an operation it lacks: that would mean it was drawn from
imagination rather than from need.

So the constraint this module is written under is the point of it: **nothing
here may add a method to `Store`.** Every operation below is `read` and
`compare_and_set`, and where that is awkward the awkwardness is recorded rather
than smoothed away, because an awkwardness recorded is evidence about the
contract and an awkwardness smoothed away is evidence about nobody.

## What the contract survived, and where it bent

**Acquire, renew and release are each one `compare_and_set`.** No operation
here needs two keys to move together, which is the refusal most likely to be
the wrong one. Here it is not.

**A renewing lease is its own predecessor, and the remedy has to allow for
that.** Its token is already in the store from the previous grant, so a
`resolve` that tested the caller's token before the key's version would report
a renew whose answer was lost as `APPLIED` on the evidence of the *earlier*
write, and the holder would be told it had time it did not have. The token
identifies a writer, not a write. `resolve` in `contract.py` tests the version
first, which property 1 makes decisive, and both
`tests/conformance/test_ambiguity.py` and `tests/test_lease.py` pin the case.
The contract needs nothing more for it: the information is in the store, and
only the order of reading it matters.

**`resolve` answers the question the contract asks, not the one a lease
asks.** It returns a verdict and discards the version it read, and the version
is exactly what a lease needs next -- it is the fencing token, and a grant
without one cannot be renewed. So the ambiguous path pays a second read. That is
a round trip, not a missing operation, and it is on the rare path; the contract
stands. Recorded because it is the closest this consumer came to falsifying it.

**Property 4's remedy reaches a deletion only half way, and the conformance
suite does not ask it to reach further.** `resolve` decides `APPLIED` by finding
the caller's token in the store, and a delete removes the record that carries
it. So an ambiguous *release* that **did** apply is indistinguishable from one
whose key was taken by somebody else -- both leave the key moved with our token
gone, and both report `SUPERSEDED`. (The other direction is clean: a release
that did not land leaves the version unmoved and reports `NOT_APPLIED`.) **This
lease does not need the distinction**, because both readings mean the caller no
longer holds the key, which is the only question `release` asks. So it is not a
defect this consumer can claim, and it is a real edge for any consumer that does
need the distinction, which is why it is written here and pinned by a test
rather than left to be discovered.

## What is deliberately not here

**A pool.** `ration` promises a bounded claim on a finite shared *pool*, and
this module grants one holder per key. The pool is not here because it is in
`pool.py`, built on these leases: a pool of N grants is N keys, each slot one of
these leases, because expiry is evaluated by the store and the store expires
keys -- `compare_and_set` takes one `ttl` for one key -- rather than elements
inside a value. `pool.py` states that argument in full, and what building the
pool on it found.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ration.contract import (
    Ambiguous,
    Applied,
    Record,
    Resolution,
    Store,
    resolve,
)

__all__ = ["Lease", "acquire", "release", "renew"]


@dataclass(frozen=True, slots=True)
class Lease:
    """A granted claim, and everything needed to extend or end it.

    Frozen because a lease is a fact about a moment. `renew` returns a new one
    rather than moving this one forward: a caller holding a stale `Lease` is
    holding a stale fencing token, and that should look like stale data rather
    than like an object that quietly aged.
    """

    key: str
    #: The holder's identity, and the token written into the store. Property 4
    #: is what makes this load-bearing rather than decorative -- it is how a
    #: caller recognises its own write after the answer was lost.
    holder: str
    #: The version the grant landed at. **This is the fencing token.** A
    #: holder that stalled past its expiry and resumes still carries the version
    #: it was granted at, and every later operation is a compare-and-set from
    #: it, so a store that has moved on refuses it. The lease does not have to
    #: know it stalled.
    version: int
    #: What was asked for, kept so `renew` can default to it.
    ttl: float | None
    payload: bytes = b""

    def _record(self) -> Record:
        return Record(self.payload, self.holder)


def acquire(
    store: Store,
    key: str,
    holder: str,
    *,
    ttl: float | None = None,
    payload: bytes = b"",
) -> Lease | None:
    """Take `key` if it is free. Return `None` if it is not.

    **A refusal is not retried here, and that is a decision rather than an
    omission.** A caller that wants to wait writes the loop, because only the
    caller knows whether waiting is better than failing. Retrying inside this
    function would turn "I hold the lease" and "I eventually held the lease"
    into one return value, and this package's error direction is to refuse.

    `Unreachable` is **not caught**. Property 3 makes it an exception precisely
    so a caller cannot ignore it by only checking for the outcome it wanted, and
    catching it here to return `None` would convert "the store is gone" into
    "the lease is taken" -- a fail-open dressed as a refusal, which is what
    `fail-open` exists in the suite to fail.

    **Idempotent for a holder that reuses its identity.** If the key is
    already held under this exact token, the existing grant is returned rather
    than refused: a caller retrying after a lost answer is asking whether it
    holds the key, and it does. A caller wanting one grant per attempt passes a
    fresh token each time, and then this branch never fires.
    """
    reading = store.read(key)
    if reading.record is not None:
        if reading.record.token == holder:
            # Already ours. Whatever produced the retry, the answer to "do I
            # hold this?" is yes, and it is held at this version.
            return Lease(key, holder, reading.version, ttl, reading.record.payload)
        return None

    lease = Lease(key, holder, reading.version, ttl, payload)
    try:
        outcome = store.compare_and_set(
            key, reading.version, lease._record(), ttl=ttl
        )
    except Ambiguous:
        return _resolve_grant(store, lease)

    if isinstance(outcome, Applied):
        return replace(lease, version=outcome.version)
    # A conflict means someone moved the key between the read and the write.
    # Deliberately not retried from `outcome.version`: that field is a
    # version the key *has held*, not necessarily its current one, and
    # `contract.py` is explicit that a caller needing the current version must
    # read. Retrying from it would be building on the one number the contract
    # declines to promise.
    return None


def renew(store: Store, lease: Lease, *, ttl: float | None = None) -> Lease | None:
    """Extend `lease`. Return `None` if it has been lost.

    The compare-and-set is from the version the grant is holding, which is what
    makes this safe without the lease knowing how long it was gone.

    **Expiry alone does not break the chain; another holder does.** A lease
    whose TTL passed while nobody else took the key renews successfully, because
    expiry does not advance a version -- the key still reads at the version this
    grant landed at. That is correct rather than lenient: the version being
    unmoved is proof that no one else acquired in the window, so no grant was
    ever held twice. The moment someone does acquire, the version moves and this
    returns `None`.
    """
    extended = replace(lease, ttl=lease.ttl if ttl is None else ttl)
    try:
        outcome = store.compare_and_set(
            lease.key, lease.version, extended._record(), ttl=extended.ttl
        )
    except Ambiguous:
        return _resolve_grant(store, extended)

    if isinstance(outcome, Applied):
        return replace(extended, version=outcome.version)
    return None


def release(store: Store, lease: Lease) -> bool:
    """Give up `lease`. Return whether this call is what ended it.

    `False` means the grant was already gone -- expired and taken, or released
    twice. The caller does not hold the key either way, so `False` is
    information for a log rather than an error to handle.

    **An ambiguous release is not resolved, and returns `False`.** A delete
    removes the record carrying this caller's token, so a release that applied
    and a key that somebody else took are the same reading -- and `resolve`
    reports `SUPERSEDED` for both. `False` claims the less of the two, and every
    action it leads to is correct under either.

    **The TTL is what makes giving up safe here.** The worst state a lost
    release can leave is a grant that nobody holds and nobody ended, which is
    exactly what expiry exists to clear. A lease service obliged to *know* would
    need a remedy the contract does not offer; one that can wait needs nothing,
    and waiting is already this substrate's error direction.
    """
    try:
        outcome = store.compare_and_set(lease.key, lease.version, None)
    except Ambiguous:
        return False
    return isinstance(outcome, Applied)


def _resolve_grant(store: Store, attempted: Lease) -> Lease | None:
    """Decide what an ambiguous *grant* did, and at what version.

    Two reads on this path, and the second is the one worth explaining.
    `resolve` is the contract's shipped remedy and is used rather than
    reimplemented, so that this consumer exercises the same procedure a backend
    is judged against. It returns a verdict and discards the version it read --
    and a grant with no version has no fencing token and cannot be renewed, so
    the version has to be fetched.

    **The key can move between those two reads**, and the second read is the
    one believed. If it no longer holds this holder's token, the grant is gone
    however briefly it existed, and `None` is the honest answer.
    """
    verdict = resolve(store, attempted.key, attempted.holder, attempted.version)
    if verdict is not Resolution.APPLIED:
        return None

    reading = store.read(attempted.key)
    if reading.record is None or reading.record.token != attempted.holder:
        return None
    return replace(attempted, version=reading.version)
