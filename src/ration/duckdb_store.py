"""A candidate backend. Whether it qualifies is a conformance result, not a claim here.

Written for the same reason `postgres.py` was: the contract names no store and
leaves the choice downstream of the suite, so this module exists to be
**judged**. It is imported only by the suite; nothing in `ration` depends on it.

## Why this was written at all

A store qualifies by a conformance result, obtained by running the suite rather
than by arguing from the store's documentation, and this module is what lets the
suite run against DuckDB.

**It is also the best available test of the suite itself.** Every violator in
`tests/violators.py` is wrong *logically* -- it computes the wrong answer, and it
is wrong in one process where the suite can see it. DuckDB is different in kind:
it is capable of being **correct in one process and structurally unusable in
two**, which is precisely the caveat `tests/test_postgres_cross_process.py` was
built to remove. A backend that passes the in-process suite and cannot be
deployed is the sharpest thing this repository can point at its own instruments.

## The module is not called `duckdb.py`

`postgres.py` is named for its substrate and imports `psycopg`, so the names do
not collide. Here they would: a module named `ration/duckdb.py` that does
`import duckdb` is correct under absolute imports and breaks the moment anything
puts `src/ration` itself on `sys.path`. That failure is silent and confusing,
and matching `postgres.py`'s naming is not worth it.

## What is measured, and what is inferred

The behaviour the notes below describe was observed by **running** duckdb 1.5.5
on Windows, not read from documentation. That is one run's result: another
version or platform may differ, and where the platform may matter the note says
so.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import duckdb

from ration.contract import (
    ABSENT,
    Applied,
    Conflict,
    Outcome,
    Reading,
    Record,
    Unreachable,
)

__all__ = ["SCHEMA", "DuckDBStore"]


#: One row per key, and the row **outlives the value** -- the same shape
#: `postgres.py` uses, and for the same reason: `payload IS NULL` is how a key is
#: absent, and the row stays behind holding `version`, so a re-creation cannot
#: reuse a number. Deleting the row would recycle versions, which is what the
#: `recycling` violator exists to fail.
#:
#: `expires_at` is a **DOUBLE holding epoch seconds**, not a `TIMESTAMPTZ`.
#: That is not a style choice: on this build, any expression producing a
#: timestamp with a time zone raises `Invalid Input Error: Required module
#: 'pytz' failed to import`. A backend whose correctness depended on an optional
#: transitive Python module would be a finding in itself, so the type was chosen
#: to avoid needing one. `epoch(now())` is evaluated by the engine and needs
#: nothing.
SCHEMA = """
CREATE TABLE IF NOT EXISTS ration_keys (
    key        VARCHAR PRIMARY KEY,
    version    BIGINT NOT NULL,
    payload    BLOB,
    token      VARCHAR,
    expires_at DOUBLE
)
"""

#: Expiry as a predicate over the engine's own clock, evaluated on read.
#:
#: Property 2 forbids trusting a timestamp a client computed. There is nowhere in
#: this statement for a caller's idea of the time to enter: the deadline was
#: written by the engine from a duration, and it is compared against
#: `epoch(now())` by the engine.
_READ = """
SELECT version,
       CASE WHEN expires_at IS NOT NULL AND expires_at <= epoch(now())
            THEN NULL ELSE payload END,
       CASE WHEN expires_at IS NOT NULL AND expires_at <= epoch(now())
            THEN NULL ELSE token END
  FROM ration_keys
 WHERE key = ?
"""

#: **Two statements, not one, and that is a real divergence from `postgres.py`
#: rather than a preference.** The Postgres backend expresses the whole contract
#: as a single data-modifying CTE. DuckDB rejects that shape outright --
#: `Parser Error: A CTE needs a SELECT` -- so the update and the insert branches
#: are dispatched in Python on `expected_version == 0`.
#:
#: The dispatch is safe because the two branches are mutually exclusive **in the
#: engine, not merely in the caller**: a row's version is >= 1 from the moment it
#: exists, so `expected_version == 0` can never match the `UPDATE`, and the
#: `INSERT` is guarded by the primary key rather than by a `NOT EXISTS` this code
#: evaluated. Two creators racing both attempt the insert and exactly one commits.
_UPDATE = """
UPDATE ration_keys
   SET version    = version + 1,
       payload    = ?,
       token      = ?,
       expires_at = CASE WHEN ? IS NULL THEN NULL
                         ELSE epoch(now()) + CAST(? AS DOUBLE) END
 WHERE key = ?
   AND version = ?
RETURNING version
"""

_INSERT = """
INSERT INTO ration_keys (key, version, payload, token, expires_at)
SELECT ?, 1, ?, ?,
       CASE WHEN ? IS NULL THEN NULL ELSE epoch(now()) + CAST(? AS DOUBLE) END
ON CONFLICT DO NOTHING
RETURNING version
"""

_CURRENT_VERSION = "SELECT version FROM ration_keys WHERE key = ?"


class DuckDBStore:
    """The contract over one DuckDB file, with one cursor per calling thread.

    **The store is a FILE, and one process owns it.** That is the whole of
    what this backend is and the whole of what disqualifies it; see
    `tests/test_duckdb_single_process.py`, which asserts the refusal rather than
    describing it.

    The cursor-per-thread is DuckDB's own documented pattern -- one connection,
    `connection.cursor()` per thread -- and is not the connection pooling
    `PostgresStore` needed. There is no socket here, so there is no port to
    exhaust, which is one hazard this backend genuinely does not have.
    """

    def __init__(self, path: str | Callable[[], str]) -> None:
        #: Resolved per acquisition rather than held, for the same reason
        #: `PostgresStore` does it: it is how the suite makes this store
        #: genuinely unreachable, by pointing it somewhere that cannot be opened
        #: rather than by simulating a refusal it would like the store to make.
        self._path: Callable[[], str] = path if callable(path) else (lambda: path)
        self._lock = threading.Lock()
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._conn_path: str | None = None
        self._local = threading.local()

    # -- the contract -----------------------------------------------------

    def read(self, key: str) -> Reading:
        row = self._execute(_READ, [key]).fetchone()
        if row is None:
            # No row at all: never written. Absence is a version, and this is
            # the one it is.
            return Reading(ABSENT, None)
        version, payload, token = row
        if payload is None:
            # Deleted, or expired. Both are absence, and both keep the version.
            return Reading(version, None)
        return Reading(version, Record(payload=bytes(payload), token=token))

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        record: Record | None,
        *,
        ttl: float | None = None,
    ) -> Outcome:
        payload = record.payload if record is not None else None
        token = record.token if record is not None else None
        try:
            if expected_version == ABSENT:
                row = self._execute(
                    _INSERT, [key, payload, token, ttl, ttl]
                ).fetchone()
            else:
                row = self._execute(
                    _UPDATE, [payload, token, ttl, ttl, key, expected_version]
                ).fetchone()
        except (duckdb.TransactionException, duckdb.ConstraintException):
            # **A lost race, reported as a conflict.** DuckDB is optimistic
            # rather than blocking, and it loses a race in two different ways
            # depending on which branch ran. Both mean the same thing to the
            # contract: the statement did not apply, because each statement here
            # is its own transaction.
            #
            # `TransactionException` -- `Conflict on tuple deletion!` -- is the
            # UPDATE branch. Where Postgres makes the second writer wait on a row
            # lock and then match nothing, DuckDB aborts it outright.
            #
            # `ConstraintException` is the INSERT branch. `ON CONFLICT DO
            # NOTHING` absorbs a conflict against a **committed** row; two
            # transactions creating the same key concurrently are not that, and
            # the loser raises `Duplicate key ... violates primary key
            # constraint` instead of quietly doing nothing.
            #
            # **A race from absence is a different case from a race from a
            # seeded version**, and a suite that only seeds its key and then
            # races from that version never races the INSERT branch at all.
            # `test_two_creators_racing_from_absence_do_not_both_apply`, in
            # `tests/conformance/test_atomicity.py`, is the case that does.
            #
            # This is the one place the backend interprets rather than
            # reports, and it is load-bearing. Two exact exception types are
            # caught, and only around the atomic step; anything else escapes.
            return Conflict(self._version_now(key))
        if row is not None:
            return Applied(row[0])
        return Conflict(self._version_now(key))

    # -- plumbing ----------------------------------------------------------

    def ensure_schema(self) -> None:
        self._execute(SCHEMA, None)

    def _version_now(self, key: str) -> int:
        """The version behind a `Conflict`, read in a second statement.

        **A divergence from `PostgresStore`, and it is a divergence in the
        conflict's favour rather than a fix.** That backend reports the version
        its statement's snapshot held, which lags under concurrency and is
        documented on `Conflict` as permitted. This one reads afresh, so the
        number is usually current -- and still is not a promise, because the key
        can move again between the failed write and this read.

        So callers must not read either backend's `Conflict.version` as
        current. The contract says nothing about it, deliberately, and two
        backends disagree about it in different directions -- which is evidence
        that the contract was right to leave it unspecified.
        """
        row = self._execute(_CURRENT_VERSION, [key]).fetchone()
        return row[0] if row is not None else ABSENT

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        """This thread's cursor onto the one connection this process holds.

        DuckDB's documented concurrency pattern. The connection is shared and
        the cursors are not, which is the opposite of `PostgresStore`'s
        connection-per-thread -- and possible only because everything here is in
        one process, which is the property that disqualifies the backend.
        """
        path = self._path()
        with self._lock:
            if self._conn is not None and self._conn_path != path:
                self._close_locked()
            if self._conn is None:
                try:
                    self._conn = duckdb.connect(path)
                except (duckdb.IOException, duckdb.Error) as exc:
                    # Property 3. A caller that cannot open the store has
                    # acquired nothing, and the one wrong thing to do is hand
                    # back something that reads like an answer.
                    raise Unreachable(str(exc)) from exc
                self._conn_path = path
                self._local = threading.local()
            conn = self._conn
        cursor = getattr(self._local, "cursor", None)
        if cursor is None or getattr(self._local, "path", None) != path:
            cursor = conn.cursor()
            self._local.cursor = cursor
            self._local.path = path
        return cursor

    def _close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except duckdb.Error:
                pass
        self._conn = None
        self._conn_path = None
        self._local = threading.local()

    def close(self) -> None:
        """Release the file. A process that holds it locks every other one out."""
        with self._lock:
            self._close_locked()

    def _execute(self, sql: str, params: list[Any] | None) -> Any:
        cursor = self._cursor()
        try:
            return cursor.execute(sql, params) if params else cursor.execute(sql)
        except duckdb.TransactionException:
            # Belongs to the caller: `compare_and_set` translates it, and
            # nothing else in this module runs a statement that can raise it.
            raise
        except duckdb.IOException as exc:
            # Reached the file and lost it. Still a refusal.
            with self._lock:
                self._close_locked()
            raise Unreachable(str(exc)) from exc
