"""Versioned published state: a generation pointer over the store.

This is a **consumer** of the contract, beside `lease.py` (one holder per key)
and `pool.py` (a bounded number over a set of them) -- and like both of those,
it adds **no method to `Store`**. It puts a version number on a publish/consume
protocol: without one, a reader cannot tell a stale artefact from a current one
except by luck. Here the generation *is* the store's version -- the same number
`addressing.py` carries as `Address.version` -- so staleness is a comparison
rather than a guess, and the generation doubles as the fencing token: a
publisher that stalled past its moment still advances from the generation it
last saw, and a store that has moved on refuses it.

**Why it lives in this repository.** The code is decomposed by state
ownership -- the substrate and the services share a repository because they own
the same state -- and the pointer, which generation is current, is a single key
under compare-and-set with a monotonic version. In state-ownership terms that is
indistinguishable from a lease: same store, same key shape, same refusal.

**The artefact half is not here, because it has nothing to coordinate.** Once
published bytes are addressable -- named by content, not location -- they are
immutable, and immutable data has nothing to coordinate. Lockfile and
atomic-swap machinery exist only because an artefact mutates in place; without
that, the whole coordination problem collapses onto the pointer. So `artefact`
below is an opaque reference this module never parses, exactly
the stance `addressing.py` takes toward an endpoint: the moment it has an
opinion about the format, it has an opinion about the artefact store, which is
the producer's concern.

## Not a lease, and the differences are the design

**A publication does not expire, and there is no parameter to make it.**
`announce` *requires* a TTL because an address is a claim about a live process,
and a claim that outlives its holder is the stale-name defect. A publication is
the opposite case: it is a fact about content, current until superseded, and an
expiry here would be data loss -- a pointer vanishing without a successor reads
as a topic never published. Same move as the contract's `ttl`-as-duration
signature: the property is structural, so there is no argument to pass wrong.

**There is no retract.** A publication is superseded, never deleted. That is
not only scope discipline: `lease.py` records that property 4's remedy reaches
a deletion only half way -- an applied delete and a key taken by somebody else
read identically -- and that this is a real edge for any consumer that needs
the distinction. This consumer keeps that edge dormant the same way the lease
does: nothing here deletes, so every ambiguous write it can suffer is one
`resolve` fully decides.

**The first publication is not a special case.** Absence is a version
(`UNPUBLISHED`, the contract's `ABSENT`), so creating a topic is advancing it,
with the same fencing: two first publishers are two compare-and-sets from the
same generation, and one of them is refused. No bootstrap step is needed, for
the same reason no artefact lock is.

## Where this consumer leans hardest on the lease's case

**A publisher is its own predecessor as the routine case, not the edge.**
Every advance rewrites a key that already holds this publisher's token, so an
ambiguous advance is exactly the case `resolve` tests the version before the
token for. The lease meets it only on renew; here it is
every write after the first. And it is one notch sharper: the token identifies
a *writer*, and a publisher rewrites its key with **different payloads**, so
after `resolve` says our write applied, the follow-up read checks the artefact
as well as the token before handing the caller a `Publication` -- a handle
naming artefact B on the evidence of a record holding artefact A would be
wrong in the one field a reader acts on. The case where that check decides is
real and is pinned in `tests/test_publish.py`: a publisher identity racing
itself, which two replicas of one producer or a retry racing its original both
are.

**And there is a case neither token nor artefact can decide.** A recovery
built purely on the follow-up read passes every test in which the lost write
would have left something distinguishable in the store. An ambiguous
**republish of the artefact already standing** leaves nothing distinguishable
-- same token, same payload -- and the only witness that the write never landed
is the version not having moved, which is `resolve`'s NOT_APPLIED doing the
deciding on its own. `tests/test_publish.py` pins that case, and it is what
holds the recovery order here -- verdict first, then the follow-up read -- in
place.

**Consumers poll, and that is the contract's refusal surfacing here.** The
contract offers no watch, so "has the pointer moved?" is a read and a
comparison, repeated. This module only ever promised that staleness is
*detectable* -- the generation does that -- not that it is *pushed*. Recorded
as a cost rather than smoothed over, because a cache added here to hide it
would be the exact defect `addressing.py` documents refusing.

## The reopen condition

If the pointer ever needs **two keys that move together** -- an artefact and
its index, published atomically -- that is a guarantee the contract
deliberately refuses, and the move is a change to the contract, not a quiet
widening here. It is written here so the person editing this file meets it
before they need it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ration.contract import (
    ABSENT,
    Ambiguous,
    Applied,
    Record,
    Resolution,
    Store,
    resolve,
)

__all__ = ["TOPIC_PREFIX", "UNPUBLISHED", "Publication", "current", "key_for", "publish"]

#: Namespaced for the reason `ROLE_PREFIX` is: two consumers agreeing on a key
#: by accident is a bug that presents as an unrelated service losing its state.
TOPIC_PREFIX = "topic/"

#: The generation of a topic never published. Absence is a version -- the
#: contract's `ABSENT` -- which is what makes the first publication an ordinary
#: advance rather than a bootstrap step with its own rules.
UNPUBLISHED = ABSENT


@dataclass(frozen=True, slots=True)
class Publication:
    """A pointer that was current at a generation, held by whoever read or made it.

    Frozen for the reason `Lease` is: a publication is a fact about a moment,
    and a caller holding a stale one should be holding stale data rather than
    an object that quietly aged. A reader and a publisher get the same type,
    because they hold the same fact -- and `generation` is the field either one
    fences with next.
    """

    topic: str
    #: Who published it, from the record's token.
    publisher: str
    #: **The fencing token.** This is the store's version for the topic's
    #: key, the same number `Address.version` carries. An advance is a
    #: compare-and-set from it, so a publisher holding a superseded generation
    #: is refused by the store rather than trusted by convention.
    generation: int
    #: The opaque reference to the published artefact. Not parsed here -- see
    #: the module docstring for why an opinion about its format would be an
    #: opinion about the artefact store.
    artefact: str


def key_for(topic: str) -> str:
    """The store key a topic's pointer lives at."""
    if not topic:
        raise ValueError("a topic needs a name")
    if topic.startswith(TOPIC_PREFIX):
        # Otherwise `key_for(key_for(t))` silently addresses a different topic,
        # and the failure is a publication that lands somewhere no reader looks.
        raise ValueError(f"topic {topic!r} is already a topic key")
    return f"{TOPIC_PREFIX}{topic}"


def publish(
    store: Store,
    topic: str,
    artefact: str,
    publisher: str,
    *,
    generation: int,
) -> Publication | None:
    """Advance `topic` to `artefact`, from `generation`. `None` if superseded.

    `generation` is required and is the discipline: it is the generation the
    caller believes is current -- from a `Publication` it holds, or
    `UNPUBLISHED` for a topic it believes has never been published. A default
    would make the unfenced write the easy path, and the unfenced write is the
    defect this module exists to close.

    **A refusal means the caller's belief is stale, and it is not retried
    here.** Whether the artefact still wants publishing over whatever landed
    meanwhile is a question about the artefacts, and only the caller can answer
    it -- so the move after a `None` is `current`, then a decision, not a loop.
    Retrying from the conflict's own version is also off the table for the
    contract's stated reason: that field is a version the key *has held*, not a
    promise about its current one.

    `Unreachable` is **not caught**, for `lease.acquire`'s reason: a store that
    is gone must not read as a publication that was refused.
    """
    if not artefact:
        raise ValueError("a publication needs something to point at")
    key = key_for(topic)
    record = Record(artefact.encode("utf-8"), publisher)
    try:
        outcome = store.compare_and_set(key, generation, record, ttl=None)
    except Ambiguous:
        return _resolve_publication(store, topic, artefact, publisher, generation)

    if isinstance(outcome, Applied):
        return Publication(topic, publisher, outcome.version, artefact)
    return None


def current(store: Store, topic: str) -> Publication | None:
    """The publication `topic` currently stands at, or `None` if it has none.

    One read, believed as of its moment. The generation in the answer is what a
    reader compares against a generation it saw earlier -- staleness is a
    comparison here, never a clock -- and what a publisher advances from next.

    `Unreachable` propagates: a store that is gone must not read as a topic
    never published, which is `resolve`'s stance in `addressing.py` and
    property 3's whole point.
    """
    reading = store.read(key_for(topic))
    if reading.record is None:
        return None
    return Publication(
        topic=topic,
        publisher=reading.record.token,
        generation=reading.version,
        artefact=reading.record.payload.decode("utf-8"),
    )


def _resolve_publication(
    store: Store, topic: str, artefact: str, publisher: str, generation: int
) -> Publication | None:
    """Decide what an ambiguous advance did, and at what generation.

    The same two-read shape as the lease's `_resolve_grant`, for its recorded
    reasons: `resolve` is the contract's shipped remedy and is used rather than
    reimplemented, and it discards the version it read, which is the one field
    a `Publication` cannot be built without.

    **Both checks below are load-bearing, and each has a pinned case the
    other cannot decide.** The verdict decides the republish of a standing
    artefact -- token and artefact both match there, and only the unmoved
    version says the write never landed. The follow-up read's artefact check
    decides a publisher racing itself -- the version moved and the token is
    ours there, and only the payload says *which* of our writes is standing.
    The token check alone, `_resolve_grant`'s shape, covers neither: the token
    identifies a writer, and a publisher rewrites its own key with different
    payloads. If either the token or the artefact differs, the pointer has
    moved past this write however briefly it stood, and `None` is the honest
    answer: the caller does not hold the current generation either way.
    """
    verdict = resolve(store, key_for(topic), publisher, generation)
    if verdict is not Resolution.APPLIED:
        return None

    reading = store.read(key_for(topic))
    if (
        reading.record is None
        or reading.record.token != publisher
        or reading.record.payload != artefact.encode("utf-8")
    ):
        return None
    return Publication(topic, publisher, reading.version, artefact)
