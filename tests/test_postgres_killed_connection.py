"""A connection the server already killed: `read()` redials once, a write never does.

Not a conformance test, for the reason `test_postgres_conflict_version.py`
gives: the contract states no property about a connection, because a connection
is this backend's plumbing and not the contract's. What is pinned here is what
a `PostgresStore` does with a dead connection it is about to reuse. It reuses
its thread's connection while `conn.closed` is false, and a connection the
server terminated still reads `closed == False` until the client next talks to
it. Without a redial, the refusal for a restart would be deferred to whoever
called next, however late that was, even though the store had been back the
whole time.

**The asymmetry is the substance.** A read cannot have applied anything, so
repeating it on a fresh connection is free. A `compare_and_set` that failed may
already have applied -- property 4 -- so it is refused and never repeated, and
the caller resolves it by reading. A test below holds each half, and the write
half is the one a well-meaning generalisation of the retry would break.

There are two arms and they are not equals.

**The real kill decides.** `pg_terminate_backend` from a second connection is
the event itself -- SQLSTATE `57P01`, *"terminating connection due to
administrator command"* -- compressed and repeatable: read, so the connection
is reused rather than fresh; kill; call again. It runs wherever a server is
named.

Nothing here matches on that string. A client that reaches the server through
a proxy can see the same kill as a socket abort carrying no SQLSTATE at all,
and what the two forms share is `OperationalError`, which is all the store keys
on. That is not a guarantee: psycopg maps a fatal result carrying no SQLSTATE
to a bare `DatabaseError`, which nothing here catches.

**The fakes are for where no server can stand.** A server cannot be made to
drop a connection between the dial and the first statement, and a run with no
`RATION_POSTGRES_DSN` has no server at all. `FaultInjector` cannot help with
either: it wraps `Store` and raises `Unreachable` itself, so it never enters
`_connect` or `_execute` and cannot express this defect at any setting.

**Every test here also runs against stores built wrong on purpose**, by the
mechanism `conftest.py` uses for the contract and for the same reason. Each test
declares the one rule it holds; each store in `_BUILDS` names the rules it
breaks and is `xfail(strict=True)` against every test holding one, so a wrong
store that passes is a failure. They are not registered in `CASES`, because
these are not contract properties and a backend is not judged on them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from ration.contract import ABSENT, Applied, Outcome, Reading, Record, Unreachable
from ration.postgres import PostgresStore, _LostOnReuse
from tests.support.backends import POSTGRES_DSN

#: Where nothing listens. The same address `backends.py` partitions with, for
#: the same reason: a DSN that failed to *parse* would prove something else.
_NOWHERE = "postgresql://ration@127.0.0.1:1/ration"

#: How long the server is given to finish killing a backend. The two-argument
#: `pg_terminate_backend` waits for the process to be gone rather than for the
#: signal to be sent -- without it the next statement can win the race against
#: its own termination, and a test about a dead connection passes on a live one.
_KILL_TIMEOUT_MS = 10_000


# -- the stores, one right and four wrong ----------------------------------

#: What this file holds the store to. A test declares exactly one.
_RULES = frozenset(
    {
        # A read on a dead reused connection is answered from a fresh one.
        "redials",
        # ...from ONE fresh one. A failed redial, or a fresh connection that
        # dies too, is a fact about the store now and is refused.
        "once",
        # A connection opened by this call and lost by it is not old news.
        "fresh-is-not-retried",
        # Property 4's half: a write is refused and never sent again.
        "a-write-is-never-repeated",
    }
)


class _NeverRedials(PostgresStore):
    """The store with no redial: every loss is just a refusal."""

    def _execute(self, sql: str, params: dict[str, Any] | None) -> Any:
        try:
            return super()._execute(sql, params)
        except _LostOnReuse as exc:
            raise Unreachable(str(exc)) from exc


class _RedialsUntilItWorks(PostgresStore):
    """The retry as a habit rather than one act. It cannot tell fresh from reused either."""

    def read(self, key: str) -> Reading:
        try:
            return super().read(key)
        except Unreachable:
            return super().read(key)


class _CallsEveryConnectionReused(PostgresStore):
    """Drops the fresh-or-reused distinction, so a loss that *is* news is retried too."""

    def _connect(self) -> tuple[Any, bool]:
        conn, _ = super()._connect()
        return conn, True


class _RepeatsWrites(PostgresStore):
    """The well-meaning one, and the generalisation property 4 forbids.

    *A dead connection is a dead connection*, so the write is given the
    read's courtesy. Every line of it looks like consistency.
    """

    def compare_and_set(
        self,
        key: str,
        expected_version: int,
        record: Record | None,
        *,
        ttl: float | None = None,
    ) -> Outcome:
        try:
            return super().compare_and_set(key, expected_version, record, ttl=ttl)
        except _LostOnReuse:
            return super().compare_and_set(key, expected_version, record, ttl=ttl)


@dataclass(frozen=True)
class _Build:
    name: str
    store: type[PostgresStore]
    violates: frozenset[str] = frozenset()


_BUILDS = [
    _Build("correct", PostgresStore),
    _Build("never-redials", _NeverRedials, frozenset({"redials", "once"})),
    _Build(
        "redials-until-it-works",
        _RedialsUntilItWorks,
        frozenset({"once", "fresh-is-not-retried"}),
    ),
    _Build(
        "calls-every-connection-reused",
        _CallsEveryConnectionReused,
        frozenset({"fresh-is-not-retried"}),
    ),
    _Build("repeats-writes", _RepeatsWrites, frozenset({"a-write-is-never-repeated"})),
]


def _holds(rule: str) -> pytest.MarkDecorator:
    """Run the test against every build, expecting failure where `rule` is broken."""
    assert rule in _RULES, rule
    return pytest.mark.parametrize(
        "build",
        [
            pytest.param(
                build,
                id=build.name,
                marks=(
                    [
                        pytest.mark.xfail(
                            strict=True,
                            reason=f"{build.name} breaks {rule} on purpose; if this "
                            "passes, the rule is not being tested",
                        )
                    ]
                    if rule in build.violates
                    else []
                ),
            )
            for build in _BUILDS
        ],
    )


def test_every_rule_has_a_store_that_breaks_it() -> None:
    """A rule no build violates is a rule only ever seen to pass."""
    assert _RULES == frozenset().union(*(build.violates for build in _BUILDS))


# -- the real kill ---------------------------------------------------------

needs_postgres = pytest.mark.skipif(
    not POSTGRES_DSN, reason="no Postgres configured; RATION_POSTGRES_DSN is unset"
)


@dataclass
class _Address:
    """The store's DSN, counting how often it is asked for.

    `_connect` resolves the DSN once per acquisition, so the count is the number
    of times the store reached for a connection -- which is how *no redial* is
    observed from outside. `cut_after(n)` lets `n` more resolutions see the real
    server and points every later one at `_NOWHERE`: the store goes away
    *between* the reuse and the redial, which is the one ordering that reaches
    the retry and then fails it.
    """

    tag: str = field(default_factory=lambda: f"ration-redial-{uuid.uuid4().hex[:8]}")
    resolutions: int = 0
    _remaining: int | None = None

    def __call__(self) -> str:
        self.resolutions += 1
        if self._remaining is not None:
            if self._remaining == 0:
                return _NOWHERE
            self._remaining -= 1
        # Tagged so the killer can find this store's backend without reaching
        # into the store for it.
        return make_conninfo(POSTGRES_DSN, application_name=self.tag)

    def cut_after(self, resolutions: int) -> None:
        self._remaining = resolutions


def _backends(tag: str) -> list[int]:
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as observer:
        rows = observer.execute(
            "SELECT pid FROM pg_stat_activity WHERE application_name = %s", (tag,)
        ).fetchall()
    return [pid for (pid,) in rows]


def _kill(tag: str) -> int:
    """Terminate the store's one backend, wait until it is gone, and say which."""
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as killer:
        rows = killer.execute(
            "SELECT pid, pg_terminate_backend(pid, %s) FROM pg_stat_activity"
            " WHERE application_name = %s",
            (_KILL_TIMEOUT_MS, tag),
        ).fetchall()
    # Exactly one, and it died. Anything else and what follows is a test of a
    # connection nobody killed.
    assert len(rows) == 1 and rows[0][1] is True, rows
    return int(rows[0][0])


@pytest.fixture
def address() -> _Address:
    return _Address()


def _warm(build: _Build, address: _Address) -> tuple[PostgresStore, str, Applied]:
    """A store holding a connection it has already used, and a key it wrote."""
    store = build.store(address)
    store.ensure_schema()
    key = f"killed-connection/{address.tag}"
    seeded = store.compare_and_set(key, ABSENT, Record(b"seed", "seed"))
    assert isinstance(seeded, Applied)
    return store, key, seeded


@needs_postgres
@_holds("redials")
def test_a_read_on_a_killed_connection_answers_from_a_fresh_one(
    build: _Build, address: _Address
) -> None:
    store, key, seeded = _warm(build, address)
    killed = _kill(address.tag)

    # The store is up. Only this thread's connection is gone, and nothing on
    # the client has noticed.
    reading = store.read(key)

    assert reading == Reading(seeded.version, Record(b"seed", "seed"))
    # And it was answered by exactly one backend that is not the one killed, so
    # the pass is the redial rather than a kill that never landed.
    after = _backends(address.tag)
    assert len(after) == 1 and after != [killed], (killed, after)


@needs_postgres
@_holds("once")
def test_a_store_that_is_really_gone_still_refuses_after_one_redial(
    build: _Build, address: _Address
) -> None:
    store, key, _ = _warm(build, address)
    _kill(address.tag)
    before = address.resolutions
    address.cut_after(1)

    # Property 3 is not traded for the retry: the redial is a real connect to a
    # port that refuses, and a caller that cannot reach the store is told so.
    with pytest.raises(Unreachable):
        store.read(key)

    # Once for the dead connection it reused, once for the redial, and no more.
    # One is `never-redials`. Three is `redials-until-it-works`, redialling a
    # redial that was refused.
    assert address.resolutions - before == 2


@needs_postgres
@_holds("a-write-is-never-repeated")
def test_a_write_on_a_killed_connection_is_refused_and_not_repeated(
    build: _Build, address: _Address
) -> None:
    store, key, seeded = _warm(build, address)
    _kill(address.tag)
    before = address.resolutions

    # The store is up, a fresh connection would take this write, and it is
    # refused anyway. The client cannot tell *killed before the statement* from
    # *killed after it applied*; only the first is safe to repeat, so neither is.
    with pytest.raises(Unreachable):
        store.compare_and_set(key, seeded.version, Record(b"again", "again"))

    assert address.resolutions - before == 1, "the write reached for a second connection"
    assert _backends(address.tag) == [], "the write redialled"

    # What a caller does instead: read. The read opens its own connection, and
    # here it finds the write never arrived.
    assert store.read(key) == Reading(seeded.version, Record(b"seed", "seed"))


# -- where no server can stand ---------------------------------------------


class _Cursor:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _Connection:
    """Answers each statement with the next row, or raises the next error.

    `closed` stays false until the *client* closes it, whatever the server did,
    because that is the whole defect: there is nothing to check before use.
    """

    def __init__(self, *answers: tuple[Any, ...] | Exception) -> None:
        self._answers = list(answers)
        self.statements = 0
        self.closed = False

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> _Cursor:
        self.statements += 1
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Cursor(answer)

    def close(self) -> None:
        self.closed = True


class _Dialler:
    """Stands in for `psycopg.connect`, handing out what the test scripted."""

    def __init__(self, *outcomes: _Connection | Exception) -> None:
        self._outcomes = list(outcomes)
        self.dials = 0

    def __call__(self, *args: object, **kwargs: object) -> _Connection:
        self.dials += 1
        # An AssertionError on purpose: nothing in the store catches one, so a
        # dial the test did not script cannot be translated into a refusal and
        # read as the expected result.
        assert self._outcomes, "the store dialled more often than this test allows"
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _admin_shutdown() -> psycopg.OperationalError:
    error = psycopg.errors.AdminShutdown(
        "terminating connection due to administrator command"
    )
    assert error.sqlstate == "57P01"
    return error


_ROW = (7, b"payload", "token")
_READING = Reading(7, Record(b"payload", "token"))


def _store(
    build: _Build, monkeypatch: pytest.MonkeyPatch, dialler: _Dialler
) -> PostgresStore:
    monkeypatch.setattr(psycopg, "connect", dialler)
    return build.store("postgresql://scripted/ration")


@_holds("redials")
def test_scripted_a_dead_reused_connection_is_redialled_once(
    build: _Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = _Connection(_ROW, _admin_shutdown())
    fresh = _Connection(_ROW)
    dialler = _Dialler(dead, fresh)
    store = _store(build, monkeypatch, dialler)
    assert store.read("k") == _READING  # now the connection is reused, not fresh

    assert store.read("k") == _READING

    assert dead.closed, "a broken connection must not be handed out again"
    assert (dialler.dials, fresh.statements) == (2, 1)


@_holds("once")
def test_scripted_a_failed_redial_refuses(
    build: _Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = _Connection(_ROW, _admin_shutdown())
    dialler = _Dialler(dead, psycopg.OperationalError("connection refused"))
    store = _store(build, monkeypatch, dialler)
    store.read("k")

    with pytest.raises(Unreachable, match="connection refused"):
        store.read("k")

    assert dialler.dials == 2


@_holds("once")
def test_scripted_the_redial_is_one_not_a_loop(
    build: _Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fresh connection dies too. That is a store in trouble, not a stale socket."""
    dead = _Connection(_ROW, _admin_shutdown())
    also_dead = _Connection(_admin_shutdown())
    dialler = _Dialler(dead, also_dead, _Connection(_ROW))
    store = _store(build, monkeypatch, dialler)
    store.read("k")

    with pytest.raises(Unreachable):
        store.read("k")

    assert dialler.dials == 2
    assert also_dead.closed


@_holds("fresh-is-not-retried")
def test_scripted_a_fresh_connection_that_fails_is_not_retried(
    build: _Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opened in this call and lost in this call: the store went away *now*.

    The retry exists for a failure that is old news. This one is not, and the
    real arm cannot stage it -- there is no seam between the dial and the first
    statement for a server to be killed in.
    """
    dialler = _Dialler(_Connection(_admin_shutdown()), _Connection(_ROW))
    store = _store(build, monkeypatch, dialler)

    with pytest.raises(Unreachable):
        store.read("k")

    assert dialler.dials == 1


@_holds("a-write-is-never-repeated")
def test_scripted_a_write_on_a_dead_reused_connection_is_never_repeated(
    build: _Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = _Connection(_ROW, _admin_shutdown())
    fresh = _Connection((8, True))
    dialler = _Dialler(dead, fresh)
    store = _store(build, monkeypatch, dialler)
    store.read("k")

    with pytest.raises(Unreachable):
        store.compare_and_set("k", 7, Record(b"again", "again"))

    assert (dialler.dials, fresh.statements) == (1, 0)
