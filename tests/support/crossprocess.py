"""A worker that contends for one key from its own interpreter.

**The point of this module is that it is a module.** The threaded hammer in
`tests/conformance/test_atomicity.py` closes over a store instance; a process
cannot. Everything a worker needs has to survive being pickled and re-imported
under `spawn`, which is what forces each worker to build its **own** store from
nothing but a DSN -- and that, rather than the thread count, is the property
this probe adds. See `tests/test_postgres_cross_process.py` for what it decides.

Nothing here imports `pytest`. A child process that imported the test framework
would be a second way for a run to fail that has nothing to do with the store.
"""

from __future__ import annotations

import os
from typing import Callable

from ration.contract import Applied, Record, Store
from ration.postgres import PostgresStore
from tests.violators import NaivePostgresStore

__all__ = [
    "STORES",
    "announce_role",
    "duckdb_open_attempt",
    "duckdb_store_attempt",
    "increment",
    "renew_with_version",
    "resolve_role",
    "worker_result",
]

#: Named rather than passed, because a class is not reliably picklable across a
#: `spawn` boundary on every platform and a string is. The names match the
#: registry in `tests/support/backends.py` on purpose: a reader comparing this
#: probe's verdict against the threaded hammer's should not have to translate.
STORES: dict[str, Callable[[str], Store]] = {
    "postgres": PostgresStore,
    "postgres-naive": NaivePostgresStore,
}

#: A retry budget, not a timeout, for the same reason `test_atomicity.py` gives:
#: an unbounded loop against a store that never applies is a hung job reported
#: as nothing at all. Larger than the threaded hammer's because a loser here is
#: contending with processes that do not yield to it.
MAX_RETRIES = 50_000


def increment(
    dsn: str, store_kind: str, key: str, iterations: int, worker_id: int
) -> str | None:
    """Read-modify-CAS `iterations` times. Returns an error, or `None`.

    **The return value is a string rather than an exception.** An exception
    raised here crosses the process boundary only if it pickles, and a
    `psycopg` error carrying a connection does not. A worker that failed for an
    interesting reason and then failed to say why would be indistinguishable
    from one that passed.
    """
    try:
        store = STORES[store_kind](dsn)
        for i in range(iterations):
            for _ in range(MAX_RETRIES):
                reading = store.read(key)
                if reading.record is None:
                    return f"worker {worker_id} found key {key!r} absent at attempt {i}"
                nxt = str(int(reading.record.payload) + 1).encode()
                outcome = store.compare_and_set(
                    key, reading.version, Record(nxt, f"p{worker_id}-{i}")
                )
                if isinstance(outcome, Applied):
                    break
            else:
                return f"worker {worker_id} never applied in {MAX_RETRIES} tries"
    except BaseException as exc:  # noqa: BLE001 -- carried home as text, see above
        return f"worker {worker_id} raised {type(exc).__name__}: {exc}"
    return None


def duckdb_open_attempt(path: str) -> tuple[int, str]:
    """Try, from this interpreter, to open a DuckDB file another process holds.

    **Deliberately raw `duckdb.connect`, not `DuckDBStore`.** The question is
    whether the ENGINE permits a second process, and routing it through this
    repository's own class would leave the answer open to the reply that the
    wrapper was written badly. `DuckDBStore` is asked the same question
    separately, and the two answers are compared in the test.

    Returns this worker's PID and a verdict string, for the same reason
    `increment` returns text: a DuckDB exception is not reliably picklable back
    across a `spawn` boundary, and a worker that failed interestingly and then
    failed to say why is indistinguishable from one that passed.
    """
    import duckdb  # noqa: PLC0415 -- must import in the CHILD, after spawn

    try:
        con = duckdb.connect(path)
    except BaseException as exc:  # noqa: BLE001 -- carried home as text
        return os.getpid(), f"REFUSED {type(exc).__name__}: {exc}"
    try:
        con.execute("SELECT count(*) FROM ration_keys").fetchone()
        return os.getpid(), "OPENED"
    finally:
        con.close()


def duckdb_store_attempt(path: str) -> tuple[int, str]:
    """The same question, asked through `DuckDBStore`, so the two can be compared."""
    from ration.contract import Unreachable  # noqa: PLC0415
    from ration.duckdb_store import DuckDBStore  # noqa: PLC0415

    try:
        DuckDBStore(path).read("probe")
    except Unreachable as exc:
        return os.getpid(), f"UNREACHABLE {exc}"
    except BaseException as exc:  # noqa: BLE001 -- carried home as text
        return os.getpid(), f"RAISED {type(exc).__name__}: {exc}"
    return os.getpid(), "READ"


def worker_result(args: tuple[str, str, str, int, int]) -> tuple[int, str | None]:
    """`Pool.map` shim that also reports the PID it actually ran in.

    **The PID is not decoration.** A `spawn` pool that silently fell back to
    running everything in the parent would produce exactly the green this probe
    is supposed to earn, and nothing else in the result would show it. The test
    asserts on the number of distinct PIDs it gets back, so this value is the
    calibration for the harness rather than for the store.
    """
    dsn, store_kind, key, iterations, worker_id = args
    return os.getpid(), increment(dsn, store_kind, key, iterations, worker_id)


# -- role addressing, from separate interpreters -------------------------------
#
# **Why these exist as module-level functions.** `tests/test_addressing.py`
# proves a role can move; every one of its tests does so inside ONE interpreter,
# sharing one store object. Addressing exists so that a role is never pinned to
# a host by accident, which makes its whole subject a role moving between
# *hosts*, and the nearest population this repository can actually create is
# processes -- each building its own store from nothing but a DSN, exactly as a
# second host would.
#
# **A process is not a host, and this does not pretend otherwise.** What it
# removes is the possibility that a handoff works only because both parties
# share in-process state. What it cannot reach is the network partition a real
# second host introduces. See `tests/test_addressing_cross_process.py`.


def announce_role(
    dsn: str, role: str, endpoint: str, holder: str, ttl: float
) -> tuple[int, str]:
    """Claim `role` from this interpreter. Returns (pid, verdict).

    Verdict is `GRANTED <version>` or `REFUSED`, as text -- a `psycopg` error
    does not pickle back across a `spawn` boundary, and a worker that failed
    interestingly and then failed to say why is indistinguishable from one that
    passed.
    """
    from ration.addressing import announce  # noqa: PLC0415

    try:
        lease = announce(PostgresStore(dsn), role, endpoint, holder, ttl=ttl)
    except BaseException as exc:  # noqa: BLE001 -- carried home as text
        return os.getpid(), f"RAISED {type(exc).__name__}: {exc}"
    if lease is None:
        return os.getpid(), "REFUSED"
    return os.getpid(), f"GRANTED {lease.version}"


def resolve_role(dsn: str, role: str) -> tuple[int, str]:
    """Ask where `role` is, from an interpreter that never held it.

    Verdict is `ABSENT` or `AT <endpoint> <holder> <version>`.
    """
    from ration.addressing import resolve  # noqa: PLC0415

    try:
        address = resolve(PostgresStore(dsn), role)
    except BaseException as exc:  # noqa: BLE001 -- carried home as text
        return os.getpid(), f"RAISED {type(exc).__name__}: {exc}"
    if address is None:
        return os.getpid(), "ABSENT"
    return (
        os.getpid(),
        f"AT {address.endpoint} {address.holder} {address.version}",
    )


def renew_with_version(
    dsn: str, role: str, holder: str, version: int, ttl: float
) -> tuple[int, str]:
    """Try to extend a grant using the version this caller was granted at.

    **This is the fencing token being spent.** The caller is reconstructed
    from primitives rather than handed a `Lease`, because that is the honest
    shape: a process that stalled and resumed has whatever it wrote down, not a
    live object. If the role has moved on, the compare-and-set from the old
    version must fail.

    Verdict is `RENEWED <version>` or `REFUSED`.
    """
    from ration.addressing import key_for  # noqa: PLC0415
    from ration.lease import Lease, renew  # noqa: PLC0415

    stale = Lease(
        key=key_for(role), holder=holder, version=version, ttl=ttl, payload=b""
    )
    try:
        extended = renew(PostgresStore(dsn), stale, ttl=ttl)
    except BaseException as exc:  # noqa: BLE001 -- carried home as text
        return os.getpid(), f"RAISED {type(exc).__name__}: {exc}"
    if extended is None:
        return os.getpid(), "REFUSED"
    return os.getpid(), f"RENEWED {extended.version}"
