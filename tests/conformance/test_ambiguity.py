"""Property 4: an ambiguous write must be resolvable by reading.

A `compare_and_set` lost after transmission has an outcome the caller has not
seen, not an outcome that does not exist. The contract's remedy is that values
carry a caller-generated token, so the caller can re-read and learn which happened
rather than guessing. Violated by `token-dropping`.

Both directions are exercised, and that is what stops a resolver which always
answers the same way from looking correct. The direction that costs something is
the one where the write *did* apply: a caller who assumes failure there retries
a write it has already made.

**A caller can be its own predecessor.** A case that starts from `ABSENT` --
the key is created and never rewritten -- has a caller's token in the store only
if the write under resolution put it there. That coincidence hides a real
defect: a `resolve` that tests the token before the version is right for a
caller writing a key once and wrong for one rewriting a key it already holds,
because its token is already there from the earlier write.

A conformance suite that decides whether a backend qualifies should not depend
on a consumer's tests to defend the remedy it ships, so
`test_a_rewriters_lost_answer_is_resolved` below reaches that case here.
"""

from __future__ import annotations

import pytest

from ration.contract import ABSENT, Ambiguous, Applied, Record, Resolution, resolve
from tests.support.backends import BackendUnderTest


@pytest.mark.conformance("ambiguity-resolvable")
def test_a_lost_answer_is_recovered_by_reading(backend: BackendUnderTest) -> None:
    store = backend.store

    # Applied, and then the answer was lost on the way back.
    landed = backend.key()
    with backend.ambiguity(applied=True):
        with pytest.raises(Ambiguous):
            store.compare_and_set(landed, ABSENT, Record(b"a", "caller-a"))
    assert resolve(store, landed, "caller-a", ABSENT) is Resolution.APPLIED, (
        "the caller's own write is in the store and the store cannot say so, "
        "so a caller who did acquire the lease is told it did not"
    )

    # Lost before the store ever saw it.
    never = backend.key()
    with backend.ambiguity(applied=False):
        with pytest.raises(Ambiguous):
            store.compare_and_set(never, ABSENT, Record(b"b", "caller-b"))
    assert resolve(store, never, "caller-b", ABSENT) is Resolution.NOT_APPLIED

    # And a caller whose write was overtaken learns that too, rather than
    # reading someone else's value and taking it for its own.
    taken = backend.key()
    winner = store.compare_and_set(taken, ABSENT, Record(b"winner", "caller-c"))
    assert resolve(store, taken, "caller-d", ABSENT) is Resolution.SUPERSEDED
    assert winner is not None


@pytest.mark.conformance("ambiguity-resolvable")
def test_a_rewriters_lost_answer_is_resolved(backend: BackendUnderTest) -> None:
    """The caller already holds the key, and writes it again.

    **The token identifies a writer, not a write.** Once a caller has written
    a key, its token is in the store regardless of what the *next* write did, so
    the token alone can no longer answer the question. Only the version can:
    property 1 says every write advances it, so a key still standing at the
    caller's expected version has not been written by anybody.

    This is the case the rest of this file cannot reach, because everything else
    here creates a key rather than rewriting one.
    """
    store = backend.store

    # A rewrite that did NOT land. The caller's token is sitting in the store
    # from the FIRST write, and a resolver reading the token first will call
    # this APPLIED and hand back time that was never granted.
    stalled = backend.key()
    first = store.compare_and_set(stalled, ABSENT, Record(b"one", "rewriter"))
    assert isinstance(first, Applied)
    with backend.ambiguity(applied=False):
        with pytest.raises(Ambiguous):
            store.compare_and_set(stalled, first.version, Record(b"two", "rewriter"))
    assert resolve(store, stalled, "rewriter", first.version) is Resolution.NOT_APPLIED, (
        "the caller's own token was read as evidence of a write that never "
        "happened, so a rewriter is told its second write landed when only its "
        "first one did -- the defect this case exists to catch"
    )

    # And the mirror, so a resolver that always answers NOT_APPLIED for a
    # rewriter cannot pass the case above by being uniformly pessimistic.
    landed = backend.key()
    start = store.compare_and_set(landed, ABSENT, Record(b"one", "rewriter"))
    assert isinstance(start, Applied)
    with backend.ambiguity(applied=True):
        with pytest.raises(Ambiguous):
            store.compare_and_set(landed, start.version, Record(b"two", "rewriter"))
    assert resolve(store, landed, "rewriter", start.version) is Resolution.APPLIED, (
        "a rewrite that did land was reported as not having landed, so a caller "
        "retries a write it has already made"
    )
