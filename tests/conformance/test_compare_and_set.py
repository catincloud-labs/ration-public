"""Property: the write lands if and only if the key is still where the caller left it.

The contract states this as the operation rather than as one of its four
numbered properties, which makes it the easiest thing in the contract to assume
rather than check. Violated by `create-always` and by `conflict-writes`.

It is one test rather than several on purpose. A store expected to fail this
property is marked strict-xfail against every test carrying it, so splitting the
assertions into a test per clause would require each violator to fail all of
them -- and `create-always` is correct about deletion, which is precisely why it
is a useful violator. The property is the unit the suite reports on.
"""

from __future__ import annotations

import pytest

from ration.contract import ABSENT, Applied, Conflict, Record
from tests.support.backends import BackendUnderTest


@pytest.mark.conformance("compare-and-set")
def test_the_write_lands_only_at_the_expected_version(
    backend: BackendUnderTest,
) -> None:
    store = backend.store
    key = backend.key()
    first = Record(payload=b"first", token="caller-a")
    second = Record(payload=b"second", token="caller-b")

    # Absence is a version. Creation is a compare-and-set against it, so a
    # creation from the wrong expectation is refused like any other write --
    # this is the assertion an upsert dressed as a compare-and-set fails.
    assert store.compare_and_set(key, 99, first) == Conflict(ABSENT)
    assert store.read(key).version == ABSENT
    assert not store.read(key).present

    created = store.compare_and_set(key, ABSENT, first)
    assert isinstance(created, Applied)
    held = store.read(key)
    assert held.record is not None
    assert held.record.payload == b"first"
    assert held.version == created.version

    # A stale expectation is refused, and the refusal carries the version the
    # key actually reached, so a caller can retry from it rather than re-read
    # and race again.
    assert store.compare_and_set(key, ABSENT, second) == Conflict(created.version)

    # And refusing did not write. A store that answers "conflict" and writes
    # anyway has told the truth and done the other thing, and the caller it
    # misled by action has no reason left to re-read.
    unchanged = store.read(key)
    assert unchanged.record is not None
    assert unchanged.record.payload == b"first"
    assert unchanged.version == created.version

    # From the version it actually reached, the same write lands.
    applied = store.compare_and_set(key, created.version, second)
    assert isinstance(applied, Applied)
    landed = store.read(key)
    assert landed.record is not None
    assert landed.record.payload == b"second"

    # Deletion is a write and gets no exemption: refused from a stale
    # expectation, and refusing does not delete.
    assert store.compare_and_set(key, created.version, None) == Conflict(
        applied.version
    )
    assert store.read(key).present

    deleted = store.compare_and_set(key, applied.version, None)
    assert isinstance(deleted, Applied)
    assert not store.read(key).present
