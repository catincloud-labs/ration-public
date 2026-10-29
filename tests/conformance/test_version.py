"""Property 1: the version rises per key and is never reused, not even after deletion.

A value-comparing swap does not satisfy this, and the difference is not
academic. Two consequences rest here: the version *is* the generation number
the published-state surface (`publish.py`) needs, and it is the fencing token that lets a protected resource reject a stalled holder waking up
with a stale claim. A reused version makes that holder indistinguishable from
the current one. Violated by `recycling` and by `global-counter`.
"""

from __future__ import annotations

import pytest

from ration.contract import ABSENT, Applied, Record
from tests.support.backends import BackendUnderTest


@pytest.mark.conformance("version-monotonic")
def test_versions_rise_per_key_and_are_never_reused(
    backend: BackendUnderTest,
) -> None:
    store = backend.store
    key = backend.key()
    issued: list[int] = []

    created = store.compare_and_set(key, ABSENT, Record(b"held-a", "caller-a"))
    assert isinstance(created, Applied)
    issued.append(created.version)
    assert created.version > ABSENT

    deleted = store.compare_and_set(key, created.version, None)
    assert isinstance(deleted, Applied)
    issued.append(deleted.version)

    # The value is gone and the version is not. This is the whole property:
    # the counter a re-creation would have to reuse is not kept alongside the
    # value that was deleted, so deleting cannot recycle it.
    after = store.read(key)
    assert not after.present
    assert after.version == deleted.version, (
        "deletion took the key's version with it, so the next creation will "
        "issue a version that has already been used as a fencing token"
    )

    recreated = store.compare_and_set(key, after.version, Record(b"held-b", "caller-b"))
    assert isinstance(recreated, Applied)
    issued.append(recreated.version)

    assert issued == sorted(issued), f"versions did not rise: {issued}"
    assert len(set(issued)) == len(issued), f"a version was issued twice: {issued}"
    assert store.read(key).version == recreated.version

    # Per key, not per store. The cheapest way to satisfy everything above is
    # one global counter, which is monotonic, never reuses anything, and makes
    # every version in the store a statement about unrelated traffic -- so a
    # holder's fencing token moves because some other key was busy.
    quiet, busy = backend.key(), backend.key()
    first_on_busy = store.compare_and_set(busy, ABSENT, Record(b"b", "caller-b"))
    assert isinstance(first_on_busy, Applied)
    for _ in range(3):
        reading = store.read(busy)
        assert isinstance(
            store.compare_and_set(busy, reading.version, Record(b"b", "caller-b")),
            Applied,
        )

    assert store.read(quiet).version == ABSENT
    first_on_quiet = store.compare_and_set(quiet, ABSENT, Record(b"q", "caller-q"))
    assert isinstance(first_on_quiet, Applied)
    assert first_on_quiet.version == first_on_busy.version, (
        "a key's first version depends on how busy a different key was, so the "
        "version is a store-wide sequence wearing a per-key name"
    )
