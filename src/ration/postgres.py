"""A candidate backend. Whether it qualifies is a conformance result, not a claim here.

The contract names no store and leaves the choice downstream of the suite, so
this module is written to be *judged* rather than to be adopted. It is imported
only by the suite; nothing in `ration` depends on it, and `pyproject.toml`
carries no runtime dependency, because a driver in the dependency list decides
by import what only a conformance run should decide.

The whole of the contract is two SQL statements. That is itself the interesting
result: the substrate is meant to be weaker than any store that implements it,
and here is the evidence -- transactions, advisory locks, `LISTEN`, and
everything else this server can do sit unused behind a table with five columns.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import psycopg

from ration.contract import (
    ABSENT,
    Applied,
    Conflict,
    Outcome,
    Reading,
    Record,
    Unreachable,
)

__all__ = ["PostgresStore", "SCHEMA"]


#: One row per key, and the row **outlives the value**.
#:
#: That is the whole of property 1. `payload IS NULL` is how a key is absent
#: -- deleted, or never written -- and the row stays behind holding `version`,
#: so the number a re-creation would have to reuse is not stored alongside the
#: value that was deleted. The natural schema, `DELETE FROM ration_keys WHERE
#: key = ...`, recycles versions; the suite registers that store as `recycling`
#: and expects it to fail.
SCHEMA = """
CREATE TABLE IF NOT EXISTS ration_keys (
    key        text PRIMARY KEY,
    version    bigint      NOT NULL,
    payload    bytea,
    token      text,
    expires_at timestamptz
)
"""

#: Expiry as a predicate over the server's own clock, evaluated on read.
#:
#: Property 2 permits a backend to satisfy this natively or by evaluating a
#: clock predicate at write time, and forbids it to satisfy it by trusting a
#: timestamp a client computed. There is nowhere in this statement for a
#: caller's idea of the time to enter: the deadline was written by the server,
#: and it is compared against `now()` by the server.
_READ = """
SELECT version,
       CASE WHEN expires_at IS NOT NULL AND expires_at <= now()
            THEN NULL ELSE payload END,
       CASE WHEN expires_at IS NOT NULL AND expires_at <= now()
            THEN NULL ELSE token END
  FROM ration_keys
 WHERE key = %(key)s
"""

#: The atomic step, and it is one statement.
#:
#: There is no read followed by a write, so there is no window between them.
#: `UPDATE ... WHERE version = %(expected)s` takes the row lock and re-evaluates
#: its own predicate against the committed row, so a second writer arriving from
#: the same version blocks, wakes to a version that has moved, and matches
#: nothing. The insert branch is guarded by the **unique index**, not by its
#: `NOT EXISTS`: two creators can both see an absent key in their snapshots, and
#: only one of them can commit the row.
#:
#: `now() + make_interval(...)` is property 2 in the write direction. The caller
#: passes a **duration**, so a deadline it computed itself has nowhere to go.
#:
#: The final branch always yields exactly one row, so a conflict is reported
#: without a second round trip.
#:
#: **That row is read from this statement's snapshot, so the version it
#: carries lags under concurrency, and this was measured rather than reasoned
#: about.** The `UPDATE` gets `EvalPlanQual` -- it waits for the concurrent
#: writer, re-checks against the committed row, and correctly matches nothing --
#: but this plain `SELECT` keeps the snapshot taken when the statement began.
#: Two writers from version 1: the loser is handed `Conflict(1)` while the key
#: is at 2. Two creators: the loser is handed `Conflict(0)` while the key is at
#: 1, because `ON CONFLICT DO NOTHING` returns no row and the `COALESCE` falls
#: through to a snapshot in which nothing exists.
#:
#: **Not a defect against the contract.** The contract requires a conflict to
#: be reported without writing, which this does; `Conflict` says what its
#: version is and is not. Making it exact needs either a second statement -- a
#: new snapshot, and the round trip this shape exists to avoid -- or `ON
#: CONFLICT DO UPDATE`, which would have to **write in order to read**, and a
#: store that writes while reporting a conflict is the thing
#: `ConflictWritesStore` exists to fail. So the cheap fix is the forbidden one,
#: which is why `Conflict` documents the lag rather than the SQL removing it.
#: Pinned by `tests/test_postgres_conflict_version.py`.
_COMPARE_AND_SET = """
WITH updated AS (
    UPDATE ration_keys
       SET version    = version + 1,
           payload    = %(payload)s,
           token      = %(token)s,
           expires_at = CASE WHEN %(ttl)s::double precision IS NULL THEN NULL
                             ELSE now() + make_interval(secs => %(ttl)s) END
     WHERE key = %(key)s
       AND version = %(expected)s
 RETURNING version
),
inserted AS (
    INSERT INTO ration_keys (key, version, payload, token, expires_at)
    SELECT %(key)s, 1, %(payload)s, %(token)s,
           CASE WHEN %(ttl)s::double precision IS NULL THEN NULL
                ELSE now() + make_interval(secs => %(ttl)s) END
     WHERE %(expected)s = 0
       AND NOT EXISTS (SELECT 1 FROM ration_keys WHERE key = %(key)s)
    ON CONFLICT (key) DO NOTHING
 RETURNING version
)
SELECT version, true AS applied FROM updated
UNION ALL
SELECT version, true            FROM inserted
UNION ALL
SELECT COALESCE((SELECT version FROM ration_keys WHERE key = %(key)s), 0), false
 ORDER BY applied DESC
 LIMIT 1
"""


class _LostOnReuse(Unreachable):
    """The statement failed on a connection this call did not open.

    Still a refusal, and everything outside this module sees it as one. It is a
    type of its own because it is the one failure that may be old news: a
    connection the server terminated reads `closed == False` until the client
    next talks to it, so *lost just now* and *lost hours ago* arrive as the
    same error on the same line. Only `read()` acts on the difference.
    """


class PostgresStore:
    """The contract over one table, with one connection per calling thread.

    The connection reuse is a **conformance requirement, not an
    optimisation.** A connection per operation is the obvious shape and reads
    as the careful one -- no pool to get wrong, no state to invalidate -- but
    under the suite's threaded probe it can run the machine out of ephemeral
    ports: every closed connection sits in `TIME_WAIT` for minutes afterwards,
    holding one, and once the range is spent a connect fails on `Address
    already in use`.

    That is worth stating plainly because of what it says about the *suite*
    rather than about Postgres. A store can pass every property and still not
    survive being asked, and nothing in the contract catches this -- there is
    no property about exhausting the machine. Only running the real thing under
    real load finds it, which is the argument for deciding a backend by
    conformance result rather than by comparison.
    """

    def __init__(self, dsn: str | Callable[[], str]) -> None:
        #: Resolved per acquisition rather than held, so a store can outlive a
        #: change of address. It is also how the suite makes this store
        #: genuinely unreachable -- pointing it somewhere that refuses, rather
        #: than simulating a refusal it would like the store to have made.
        self._dsn: Callable[[], str] = dsn if callable(dsn) else (lambda: dsn)
        self._local = threading.local()

    # -- the contract -----------------------------------------------------

    def read(self, key: str) -> Reading:
        params = {"key": key}
        try:
            cursor = self._execute(_READ, params)
        except _LostOnReuse:
            # Refusing here would defer the refusal for a store restart to
            # whoever calls next, however late, and report an outage on a store
            # that has been back the whole time. A read applied nothing
            # whatever became of the first attempt, so it is asked again --
            # once, and `_execute` has already discarded the connection, so on
            # one opened here. A second failure is therefore a fact about the
            # store now and is raised as it stands. A store that is really down
            # refuses inside `_connect`; property 3 is not traded for this.
            # Priced against a liveness check on every reuse, which is a round
            # trip per read, forever. `tests/test_postgres_killed_connection.py`
            # pins both halves: the read redials once, a write never does.
            cursor = self._execute(_READ, params)
        row = cursor.fetchone()
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
        params: dict[str, Any] = {
            "key": key,
            "expected": expected_version,
            "payload": record.payload if record is not None else None,
            "token": record.token if record is not None else None,
            "ttl": ttl,
        }
        # `_LostOnReuse` is deliberately not caught here, though the same
        # dead connection raises it. The client cannot tell a connection that
        # died before the statement left from one that died after it applied,
        # and repeating the second is a double write -- property 4. The caller
        # resolves it by reading, and the read is what redials.
        row = self._execute(_COMPARE_AND_SET, params).fetchone()
        assert row is not None, "the statement's last branch always yields a row"
        version, applied = row
        return Applied(version) if applied else Conflict(version)

    # -- plumbing ----------------------------------------------------------

    def ensure_schema(self) -> None:
        self._execute(SCHEMA, None)

    def _connect(self) -> tuple[psycopg.Connection[Any], bool]:
        """This thread's connection, opened if it has none that still serves.

        Keyed on the resolved DSN, so a store whose address changes does not
        keep talking to the old one. That is not hypothetical here: it is how
        the suite partitions this backend.

        The flag is whether the connection was **reused** rather than opened by
        this call. `conn.closed` cannot stand in for it: a connection the
        server terminated reads `closed == False` until the client next talks
        to it, so a reused connection is one whose health is simply not known.
        """
        dsn = self._dsn()
        conn: psycopg.Connection[Any] | None = getattr(self._local, "conn", None)
        if conn is not None and (getattr(self._local, "dsn", None) != dsn or conn.closed):
            self._discard()
            conn = None
        reused = conn is not None
        if conn is None:
            try:
                conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
            except psycopg.Error as exc:
                # Property 3, and the reason it is raised rather than
                # returned. A caller that cannot reach the store has acquired
                # nothing, and the one wrong thing to do here is hand back
                # something that reads like an answer.
                raise Unreachable(str(exc)) from exc
            self._local.conn = conn
            self._local.dsn = dsn
        return conn, reused

    def _discard(self) -> None:
        """Drop this thread's connection. A broken one must not be handed out again."""
        conn: psycopg.Connection[Any] | None = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except psycopg.Error:
                pass
        self._local.conn = None
        self._local.dsn = None

    def _execute(
        self, sql: str, params: dict[str, Any] | None
    ) -> psycopg.Cursor[Any]:
        conn, reused = self._connect()
        try:
            return conn.execute(sql, params)
        except psycopg.OperationalError as exc:
            # Reached the server and lost it mid-statement. Still a refusal, and
            # the connection is now suspect -- reusing it would turn one lost
            # server into every later call on this thread failing for a reason
            # that has already gone away.
            #
            # Property 4 covers the narrower case where the write may already
            # have applied. The suite injects that case directly rather than
            # hoping a connection drops at exactly the right instant.
            self._discard()
            # Or it was lost long before the statement and this is only when
            # the client found out, which a reused connection cannot rule out.
            # Said in the type, and decided by the caller: see `read()`.
            refusal = _LostOnReuse if reused else Unreachable
            raise refusal(str(exc)) from exc
