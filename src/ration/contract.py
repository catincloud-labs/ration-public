"""The coordination contract, expressed as types.

This module states the contract, and the conformance suite under
`tests/conformance/` decides whether a backend implements it. The substrate is
one key at a time under compare-and-set against a version, and nothing more.
Four properties hold of it, numbered here because the rest of this package and
the suite cite them by number:

1. The version rises per key and is never reused, not even after deletion.
2. Expiry is evaluated by the store, against its own clock, when asked.
3. A store that cannot be reached is a refusal, never a guess.
4. A write whose outcome was lost is resolvable by reading.

The module deliberately adds nothing to that: a capability the contract refuses
is not quietly available here because it was convenient to expose.

Read this file as a specification with zero implementations. That is
deliberate: an interface with one implementation is premature, and an
interface with none is a specification.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "ABSENT",
    "Ambiguous",
    "Applied",
    "Conflict",
    "Outcome",
    "Record",
    "Reading",
    "Resolution",
    "Store",
    "Unreachable",
    "resolve",
]


#: The version of a key that has never been written. Absence is a version, so
#: creation is not a special case: it is a compare-and-set against this one. A
#: backend that treats an absent key as "any expected version will do" has not
#: implemented the contract, it has implemented an upsert.
ABSENT = 0


@dataclass(frozen=True, slots=True)
class Record:
    """A value in the store, and the token of the caller who wrote it.

    The token is not decoration and is not optional. Property 4 makes an
    ambiguous write resolvable *by reading*, and reading can only answer the
    question if what comes back identifies who wrote it. A store that keeps the
    payload and discards the token has kept the part that is easy to replace.
    """

    payload: bytes
    token: str


@dataclass(frozen=True, slots=True)
class Reading:
    """What `read` returns: a version, and a record if the key holds one.

    A key that is absent, was deleted, or whose lease has expired all read the
    same way -- no record -- but each still reports the version the key has
    reached. Absence is a version.
    """

    version: int
    record: Record | None

    @property
    def present(self) -> bool:
        return self.record is not None


@dataclass(frozen=True, slots=True)
class Applied:
    """The write happened, and the key is now at this version."""

    version: int


@dataclass(frozen=True, slots=True)
class Conflict:
    """The write did not happen, because the key was not at the expected version.

    A conflict is an outcome, not an error: the caller asked a question and got
    an answer. It is reported without writing -- a store that reports a conflict
    and writes anyway has answered honestly and acted otherwise, which is worse
    than either alone.

    **`version` is a version the key has held. It is not a promise about the
    key's current version, and under concurrency it is often not the current
    one.** `PostgresStore` reports the version the caller came *from* when it
    loses a race, because the statement deciding the outcome and the statement
    reporting the version share one snapshot. Two writers from version 1 leave
    the loser holding ``Conflict(1)`` while the key is at 2, and two creators
    leave the loser holding ``Conflict(0)`` while the key is at 1.

    That is permitted rather than merely tolerated. **The contract requires
    that a conflict be reported without writing, and says nothing about the
    version it carries**, because a guarantee specified before a consumer needs
    it is specified from imagination. Making this field exact costs a second
    round trip, or an ``ON CONFLICT DO UPDATE`` that writes in order to read --
    which the paragraph above forbids outright.

    So **a caller that needs the current version must read.** Treating this
    number as current is the mistake the field invites, which is why the
    warning lives here rather than only in the backend that revealed it.
    Strengthening the contract to require an exact version is a change to the
    contract itself, and would need a consumer that actually needs it.
    """

    version: int


Outcome = Applied | Conflict


class Unreachable(Exception):
    """The store could not be reached.

    Property 3: a caller that cannot reach the store has acquired nothing and
    must not proceed. This is raised rather than returned on purpose -- an
    outcome can be ignored by a caller who only checks for the one it wanted,
    and fail-closed that depends on the caller remembering to be careful is a
    habit rather than a property of the substrate.
    """


class Ambiguous(Exception):
    """A write that may or may not have applied.

    Property 4: a `compare_and_set` that is lost after transmission has an
    outcome the caller has not seen, not an outcome that does not exist. The
    remedy is `resolve`, which reads. Guessing is not a remedy.
    """


class Store(Protocol):
    """The whole substrate. Two operations, and nothing else.

    The contract's refusals are load-bearing and are visible here as absences:
    no second key, no ordering, no watches, no queue, no lock held across a
    round trip. Adding a method to this Protocol is a change to the contract
    itself, not a convenience.
    """

    def read(self, key: str) -> Reading:
        """Return the key's version, and its record if it holds one.

        Raises `Unreachable` if the store cannot be reached.
        """
        ...

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        record: Record | None,
        *,
        ttl: float | None = None,
    ) -> Outcome:
        """Write `record` if and only if the key is still at `expected_version`.

        A `record` of `None` deletes: the key becomes absent, and its version
        advances anyway, because property 1 says versions are never reused and
        deletion is the case it names.

        `ttl` is a duration, not a deadline, and that signature is property 2
        made structural. A caller cannot pass an expiry it computed, because
        there is nowhere to put one -- the store adds the duration to its own
        clock. Passing `None` means the value does not expire.

        Raises `Unreachable` if the store could not be reached, and `Ambiguous`
        if it was reached but the outcome was not learned.
        """
        ...


class Resolution(enum.Enum):
    """What a read can tell a caller about a write it did not see land."""

    #: The caller's token is in the store. The write applied.
    APPLIED = "applied"
    #: The key has not moved since the caller's expected version. It did not.
    NOT_APPLIED = "not-applied"
    #: The key moved, and someone else's token is there. Either the write never
    #: applied or it has already been overwritten -- and the caller does not
    #: hold the key either way, which is the question actually being asked.
    SUPERSEDED = "superseded"


def resolve(
    store: Store, key: str, token: str, expected_version: int
) -> Resolution:
    """Answer, by reading, what an `Ambiguous` write did.

    This is the remedy property 4 exists to make possible, and it is shipped as
    code rather than left to each caller so that a backend is judged against the
    real procedure. It needs both halves of the contract: the token says whether
    the value is ours, and the monotonic version says whether anything moved at
    all. Neither answers alone.

    **The version is tested before the token, and that order is the whole
    correctness of this function.** The other order is wrong for any caller
    that is **its own predecessor** -- one writing a key it already holds. A
    lease renewing itself is exactly that: its token is already in the store
    from the previous grant, so with the token tested first, a renew whose
    answer was lost would resolve as `APPLIED` on the strength of a record the
    *earlier* write left behind, and the holder would be told it had time it
    did not have.

    **The token identifies a writer, not a write.** Those coincide only while a
    caller writes a key at most once, and a renewing lease or an advancing
    publisher writes it again and again; `test_ambiguity.py` reaches that case
    in `test_a_rewriters_lost_answer_is_resolved`. Property 1 is what closes
    it: every write advances the version, deletion included, so a key still
    standing at `expected_version` has not been written by anyone, and no token
    can argue otherwise.
    """
    reading = store.read(key)
    if reading.version == expected_version:
        # Nothing has been written by anyone -- not us, not a competitor. This
        # is decided before the token is consulted, because our own token may be
        # sitting there from a write that landed before the one being resolved.
        return Resolution.NOT_APPLIED
    if reading.record is not None and reading.record.token == token:
        return Resolution.APPLIED
    return Resolution.SUPERSEDED
