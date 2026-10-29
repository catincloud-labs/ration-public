"""Stores that are wrong on purpose, one property at a time.

These are the suite. A property whose violator is missing looks exactly like
a property whose violator silently passes, and neither is being tested. The rule
in `AGENTS.md` is that the violating implementation lands in the same change as
the property, and `test_the_suite_discriminates.py` is what stops that rule from
being a thing everyone remembers to follow.

Each class below is small deliberately. A store that is broken in several ways
fails everything and therefore proves nothing about which check caught it; the
value of a violator is entirely in how *little* it does wrong. Where one
unavoidably breaks a second property, it says so and says why, and it declares
both in `backends.py` rather than being marked for one and quietly failing two.
"""

from __future__ import annotations

from collections.abc import Callable

from ration.contract import (
    ABSENT,
    Applied,
    Conflict,
    Outcome,
    Reading,
    Record,
    Store,
    Unreachable,
)
from ration.memory import MemoryStore
from ration.postgres import PostgresStore

__all__ = [
    "ClientExpiryStore",
    "ConflictWritesStore",
    "CreateAlwaysStore",
    "FailOpenStore",
    "GlobalCounterStore",
    "NaivePostgresStore",
    "NaiveStore",
    "RecyclingStore",
    "TokenDroppingStore",
]


class CreateAlwaysStore(MemoryStore):
    """Violates: `compare-and-set`. Absence is not a version here.

    Creation is special-cased into an upsert: if the key holds nothing, the
    write lands whatever version the caller expected. Every other path is
    correct, which is what makes this worth having -- it is the shape a backend
    reaches by implementing creation with `INSERT ... ON CONFLICT DO NOTHING`
    and calling the job done, and the resulting store passes a suite that only
    ever compare-and-sets from versions it just read.
    """

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        record: Record | None,
        *,
        ttl: float | None = None,
    ) -> Outcome:
        self._staged()
        with self._lock:
            reading = self._read_locked(key)
            if not reading.present:
                return self._commit_locked(key, record, ttl)
            if reading.version != expected_version:
                return Conflict(reading.version)
            return self._commit_locked(key, record, ttl)


class ConflictWritesStore(MemoryStore):
    """Violates: `compare-and-set` and `atomicity`. Reports a conflict, writes anyway.

    The reply is correct and the effect is not, which is the worst available
    combination: a caller told it lost the race has no reason to re-read, so the
    damage is invisible from where it happens.

    It is declared against two properties rather than one, and that is not
    slack in the rule -- it is forced. A store that writes when it should not
    destroys the winner's value in any race it loses, so there is no interleaving
    in which it satisfies atomicity. A violator that could break only the reply
    would be preferable and does not exist.
    """

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        record: Record | None,
        *,
        ttl: float | None = None,
    ) -> Outcome:
        self._staged()
        with self._lock:
            reading = self._read_locked(key)
            applied = self._commit_locked(key, record, ttl)
            if reading.version != expected_version:
                return Conflict(reading.version)
            return applied


class NaiveStore(MemoryStore):
    """Violates: `atomicity`. Reads, then writes, with nothing joining them.

    This is the negative control the atomicity property exists to reject:
    read-then-write is exactly what the operation must not be. If it ever
    passes, the suite is measuring nothing.

    Single-threaded it is indistinguishable from a correct store, which is the
    entire difficulty. It answers every question correctly when asked one at a
    time.
    """

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        record: Record | None,
        *,
        ttl: float | None = None,
    ) -> Outcome:
        # Interaction one. The comparison is decided here, on a value that
        # stops being true the moment the lock is released.
        reading = self.read(key)
        self._staged()
        if reading.version != expected_version:
            return Conflict(reading.version)
        # Interaction two, against a store that may have moved. Nothing
        # re-checks, so the write lands on top of whatever arrived in between.
        with self._lock:
            return self._commit_locked(key, record, ttl)


class RecyclingStore(MemoryStore):
    """Violates: `version-monotonic`. Forgets a key when it is deleted.

    Deleting drops the key's counter along with its value, so the next creation
    starts again at 1 and a version that has already been issued is issued a
    second time. This is the natural implementation -- one row per key, deleted
    on delete -- and it costs every lease its fencing token: a stalled holder
    waking with version 1 is indistinguishable from the current holder.

    It is not the version violator that matters most in production. Restoring
    the store from backup replays lease versions, and a restore is an ordinary
    recovery run by someone who believes they are restoring a database. That
    path is a lifecycle event between store instances rather than one process
    misbehaving, and this suite does not reach it. The gap is stated in
    `README.md`.
    """

    def _commit_locked(
        self, key: str, record: Record | None, ttl: float | None
    ) -> Applied:
        applied = super()._commit_locked(key, record, ttl)
        if record is None:
            del self._versions[key]
        return applied


class GlobalCounterStore(MemoryStore):
    """Violates: `version-monotonic`. One counter for the store, not one per key.

    Every version it issues rises and none is ever reused, so it satisfies the
    property as most people would state it from memory. What it loses is the
    word *per key*: a holder's fencing token now moves because some unrelated
    key was busy, and two keys' versions are comparable numbers that mean
    nothing when compared.

    It is the second violator of this property because the first one --
    `recycling` -- is correct about exactly this, and a property with one
    violator is tested only as far as that violator is wrong.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        super().__init__(clock)
        self._next_version = 0

    def _commit_locked(
        self, key: str, record: Record | None, ttl: float | None
    ) -> Applied:
        self._next_version += 1
        version = self._next_version
        self._versions[key] = version
        if record is None:
            self._values.pop(key, None)
        else:
            deadline = None if ttl is None else self._expiry_clock() + ttl
            self._values[key] = (record, deadline)
        return Applied(version)


class ClientExpiryStore(MemoryStore):
    """Violates: `store-evaluated-expiry`. Asks the caller what time it is.

    The deadline is computed and compared against a clock on the caller's side
    of the boundary. On one host with one clock this is invisible; across two it
    is clock skew, and clock skew in a lease presents as the lease being held
    twice -- by one holder whose clock says it still has it, and one whose clock
    says it is free.
    """

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        client_clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(clock)
        assert client_clock is not None
        self._client_clock = client_clock

    def _expiry_clock(self) -> float:
        return self._client_clock()


class StaleClockStore(MemoryStore):
    """Violates: `store-evaluated-expiry`. Reads the store's clock, decides later.

    The second violator of this property, and the more interesting one. It never
    touches a caller's clock -- the reading it uses is the store's own, so it
    survives every amount of clock skew the suite can inject. What it does is
    fetch that reading and then evaluate the predicate somewhere the reading can
    go stale, which is what `SELECT now()` followed by a decision in the client
    actually is.

    Clock injection cannot reach it and a stepper can, which is the whole reason
    the suite has both. Parking it between the fetch and the decision, and
    moving the store's clock past the deadline while it waits, produces a lease
    reported as held after the store's own clock says it expired.
    """

    def _read_locked(self, key: str) -> Reading:
        now = self._expiry_clock()
        # Everything after this point acts on a reading of the clock rather
        # than on the clock. An arbitrary delay lands here.
        self._staged()
        version = self._versions.get(key, ABSENT)
        held = self._values.get(key)
        if held is None:
            return Reading(version, None)
        record, deadline = held
        if deadline is not None and now >= deadline:
            return Reading(version, None)
        return Reading(version, record)


class TokenDroppingStore(MemoryStore):
    """Violates: `ambiguity-resolvable`. Keeps the payload, replaces the token.

    Substituting a token of its own devising is the sort of thing a store does
    to make its rows tidy, and it looks harmless because the value survives
    intact. It removes the only means a caller has of learning what an ambiguous
    write did: reading returns a record that is theirs and does not say so, so
    the answer comes back `SUPERSEDED` when nothing superseded anything.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        super().__init__(clock)
        self._issued = 0

    def _commit_locked(
        self, key: str, record: Record | None, ttl: float | None
    ) -> Applied:
        if record is not None:
            self._issued += 1
            record = Record(payload=record.payload, token=f"store-{self._issued}")
        return super()._commit_locked(key, record, ttl)


class NaivePostgresStore(PostgresStore):
    """Violates: `atomicity`. The same defect as `naive`, in a real store.

    This is the violator that matters most. Every other implementation in the
    suite is a fake this repository wrote to be caught, so without this one the
    suite would have shown it could reject **its own fakes** and nothing else,
    and a contract that cannot discriminate between real candidates has bought
    nothing.

    It is also the honest shape of the mistake. Nobody writes the naive version
    on purpose; they write `SELECT` and then `UPDATE` because that is how the
    operation reads in English, and every single-threaded test agrees with them.
    The version column is still there, still incrementing, and still meaning
    nothing, because no statement ever compares it.

    It has no in-process seam, so the deterministic stepper cannot reach it
    and skips. The threaded probe is the only instrument that decides it, which
    makes this the one case where the false-pass rate stated in
    `test_atomicity.py` carries a verdict on its own.
    """

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        record: Record | None,
        *,
        ttl: float | None = None,
    ) -> Outcome:
        reading = self.read(key)
        if reading.version != expected_version:
            return Conflict(reading.version)
        # A second statement, against a row that has been free to move since the
        # first one committed. `WHERE key = ...` and nothing else.
        row = self._execute(
            """
            INSERT INTO ration_keys (key, version, payload, token, expires_at)
            VALUES (%(key)s, 1, %(payload)s, %(token)s,
                    CASE WHEN %(ttl)s::double precision IS NULL THEN NULL
                         ELSE now() + make_interval(secs => %(ttl)s) END)
            ON CONFLICT (key) DO UPDATE
               SET version    = ration_keys.version + 1,
                   payload    = EXCLUDED.payload,
                   token      = EXCLUDED.token,
                   expires_at = EXCLUDED.expires_at
         RETURNING version
            """,
            {
                "key": key,
                "payload": record.payload if record is not None else None,
                "token": record.token if record is not None else None,
                "ttl": ttl,
            },
        ).fetchone()
        assert row is not None
        return Applied(row[0])


class FailOpenStore:
    """Violates: `unreachable-refuses`. Treats a silent store as an empty one.

    A wrapper rather than a subclass, because that is where this defect actually
    lives: not in a store, but in the layer someone adds in front of one after a
    partition caused an outage and refusing looked like the bug. It is the exact
    inversion of what makes a store in one place safe under partition: that
    unreachable means refuse. A caller that fails open turns the store's
    placement into a lost update.
    """

    def __init__(self, inner: Store) -> None:
        self._inner = inner

    def read(self, key: str) -> Reading:
        try:
            return self._inner.read(key)
        except Unreachable:
            # "Nothing there, go ahead."
            return Reading(0, None)

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        record: Record | None,
        *,
        ttl: float | None = None,
    ) -> Outcome:
        try:
            return self._inner.compare_and_set(
                key, expected_version, record, ttl=ttl
            )
        except Unreachable:
            # Claims a write it did not make, so the caller proceeds believing
            # it holds a lease that does not exist anywhere.
            return Applied(expected_version + 1)
