"""Property 3: unreachable means refuse.

Where the store lives in one place, this is load-bearing rather than tidy. Two
nodes cannot form a quorum, so a deployment that small keeps the store on one of
them, and that placement is safe under partition only because a caller who
cannot reach the store refuses. "Unreachable means refuse" is not a fallback
there; it is the mechanism by which a store in one place cannot cause a lost
update. Violated by `fail-open`.

Note where the violator sits: in a wrapper, not in a store. Failing open is
written in the layer someone adds *in front of* a correct store, after a
partition caused an outage and refusing looked like the bug.
"""

from __future__ import annotations

import pytest

from ration.contract import ABSENT, Applied, Record, Unreachable
from tests.support.backends import BackendUnderTest


@pytest.mark.conformance("unreachable-refuses")
def test_a_caller_that_cannot_reach_the_store_acquires_nothing(
    backend: BackendUnderTest,
) -> None:
    store = backend.store
    key = backend.key()

    held = store.compare_and_set(key, ABSENT, Record(b"holder-a", "caller-a"))
    assert isinstance(held, Applied)

    with backend.partition():
        # Raised, not returned. An outcome can be ignored by a caller checking
        # only for the one it wanted; an exception the caller must handle is
        # how fail-closed stops depending on everyone remembering to be careful.
        with pytest.raises(Unreachable):
            store.read(key)
        with pytest.raises(Unreachable):
            store.compare_and_set(key, held.version, Record(b"holder-b", "caller-b"))

        # An absent key is not a licence either. A store that reports "nothing
        # there" while unreachable hands out a lease that exists nowhere.
        with pytest.raises(Unreachable):
            store.read(backend.key())

    # And the partition changed nothing. The second caller was refused, so the
    # first holder still holds it at the version it was granted.
    recovered = store.read(key)
    assert recovered.record is not None
    assert recovered.record.payload == b"holder-a"
    assert recovered.version == held.version
