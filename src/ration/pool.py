"""A pool: a bounded number of simultaneous grants over a finite set of slots.

**A pool of N grants cannot live on one key, because expiry is evaluated by
the store and the store expires keys rather than elements inside a value.**
`compare_and_set` takes one `ttl` for one key. A single key holding a table of
grants with their own deadlines would have to have those deadlines evaluated by
whoever read it -- which is `client-expiry`, a violator the suite rejects. So
**a pool is N keys, each one a lease from `lease.py`.**

**The strongest evidence for that design is what is not in this file.** There
is no `renew` here and no `release`. A pool grant *is* a lease, so
`ration.lease.renew` and `ration.lease.release` work on it unchanged, and
`tests/test_pool.py` holds that. The pool adds exactly one operation -- a
different way of choosing which key to take -- and adds nothing at all to
`Store`. A consumer needing an operation the contract lacks would falsify the
contract, and this one needs none.

## What this consumer found, and it is not in the contract

**A holder retrying after a lost answer will take a *second* slot unless the
scan looks for its own grant first, and that is new here.** `lease.acquire` is
idempotent for a holder reusing its identity, and it gets that for free: there
is one key, so the read that decides whether the key is free is the same read
that recognises the holder's own token. A pool has no such luck. The natural
implementation -- walk the slots, compare-and-set the first free one -- cannot
see that this holder already holds slot 3 while it is busy taking slot 0.

**This is not overdraw of the pool.** Capacity is still respected; N slots
grant N times whatever happens. What leaks is the *handle*: a holder whose grant
landed but whose answer was lost has no `Lease` for it, cannot release it, and
only expiry reclaims it. Taking a second slot doubles that leak and hides the
first one permanently.

So **`acquire` completes a full pass before it takes anything**, and a holder
that finds its own token anywhere in the pool is handed that grant back rather
than given a new one. That converts a leaked slot into a recovered one, which is
the same move `_resolve_grant` makes for a single lease, at pool scale.

**The price is real and belongs to the pool, not to the contract.** Acquiring
costs one read per slot rather than one read. That is not a missing operation --
a batch read would make it cheaper and would change no property -- so the
contract stands here too, and the cost is recorded rather than smoothed away.

## What is deliberately not here

**Waiting.** A pool with no free slot refuses, and the caller writes the loop.
Same reasoning as `lease.acquire`: only the caller knows whether waiting beats
failing, and folding a retry in here would turn "I have a slot" and "I
eventually had a slot" into one return value.

**A stored capacity.** Nothing writes the number of slots into the store. It is
the length of a generated key set, so there is no key that has to be kept true
and no second source to disagree with the first. **The cost is that shrinking
a pool orphans keys**: slots above the new capacity keep whatever they hold, are
never scanned again, and expire without anyone watching. Growing is free.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ration.contract import Store
from ration.lease import Lease, acquire as _acquire_slot

__all__ = ["Pool", "acquire", "holders"]


@dataclass(frozen=True, slots=True)
class Pool:
    """A named set of slots. Capacity is how many there are, and nothing else.

    Frozen for the same reason `Lease` is: a pool is a description, and a caller
    holding one that no longer matches the key set should be holding stale data
    rather than an object that quietly changed shape.
    """

    name: str
    capacity: int

    def __post_init__(self) -> None:
        if self.capacity < 1:
            # A pool of zero refuses everything, which is indistinguishable from
            # a pool that is full and would hide a configuration mistake behind
            # a plausible-looking refusal.
            raise ValueError(f"a pool needs at least one slot, got {self.capacity}")

    def slot(self, index: int) -> str:
        """The key for one slot.

        The separator is deliberate and boring. Slot keys have to be derivable
        from the name alone by every process that uses the pool, because there
        is nowhere to look them up.
        """
        return f"{self.name}/{index}"

    def slots(self) -> tuple[str, ...]:
        return tuple(self.slot(i) for i in range(self.capacity))


def _order(pool: Pool, holder: str) -> range:
    """Slot indices to try, rotated by holder.

    In-order scanning is correct and contends badly: every claimant races for
    slot 0, the losers all race for slot 1, and so on. Rotating the start spreads
    that out. **No property depends on this** -- exclusivity is the store's, and
    a rotation only changes who collides with whom.

    **`hash()` would be a bug here, not a shortcut.** Python salts string
    hashing per process, so two processes sharing a pool would disagree about
    where a holder starts, and the same holder would rotate differently across
    restarts. Neither breaks correctness, which is exactly what would make it
    survive review. A stable digest costs nothing and means the rotation is a
    property of the holder rather than of the interpreter that ran it.
    """
    digest = hashlib.blake2b(holder.encode("utf-8"), digest_size=8).digest()
    start = int.from_bytes(digest, "big") % pool.capacity
    return range(start, start + pool.capacity)


def acquire(
    store: Store,
    pool: Pool,
    holder: str,
    *,
    ttl: float | None = None,
    payload: bytes = b"",
) -> Lease | None:
    """Take a slot in `pool`. Return `None` if every slot is held.

    **A slot freed during the scan is missed rather than taken.** The pass
    reads slots one at a time, so a slot released behind the read is not seen
    until the next call. That over-refuses, which is the direction the name
    promises: rationing refuses rather than overdraws.

    `Unreachable` is **not caught**, for the reason `lease.acquire` gives: a
    store that is gone must not be reported as a pool that is full.
    """
    free: str | None = None

    # A full pass before taking anything. The first free slot is remembered and
    # not taken yet, because a slot held by this holder further along the scan
    # outranks it -- see this module's docstring for why that is the whole point
    # rather than an optimisation.
    for index in _order(pool, holder):
        key = pool.slot(index % pool.capacity)
        reading = store.read(key)

        if reading.record is None:
            if free is None:
                free = key
            continue

        if reading.record.token == holder:
            # Already ours, somewhere in the pool. Hand back the grant that
            # exists rather than issuing a second one.
            return Lease(key, holder, reading.version, ttl, reading.record.payload)

    if free is None:
        return None

    # Delegated, not reimplemented. `lease.acquire` owns the ambiguity
    # handling, the fencing token and the conflict semantics, and a second
    # implementation of those here would be a second place for each of them to
    # be wrong. It re-reads the slot, which is one wasted read on the common
    # path, and avoiding the two-implementation risk is worth that read.
    #
    # A conflict is **not** retried against the next free slot. The pass is
    # already stale by then, so a retry would be guessing from older information
    # than the caller has; the caller can simply call again.
    return _acquire_slot(store, free, holder, ttl=ttl, payload=payload)


def holders(store: Store, pool: Pool) -> dict[str, str]:
    """Which slot each current holder has, keyed by slot.

    For operators and for tests. **It is a sample, not a snapshot** -- the
    slots are read one at a time and the pool can move underneath, so two slots
    in the result were never necessarily held at the same instant. Anything that
    needs a consistent count of a moving pool needs a guarantee this substrate
    does not offer, and inventing one here would be a guarantee the contract
    refuses.
    """
    found: dict[str, str] = {}
    for key in pool.slots():
        reading = store.read(key)
        if reading.record is not None:
            found[key] = reading.record.token
    return found
