"""Every implementation the suite runs against, and what each is expected to fail.

The suite's job is not to validate a backend. It is to **discriminate between
backends**, so the registry below is the suite's actual subject: one store that
should pass everything, a set that should each fail a named property and pass
the rest, and the backends that are not fakes at all.

A new backend is added here and runs what is already written. It does not get
tests of its own, and if it needs a special case to pass, that is a finding
about the backend or the contract rather than a fixture to write.

## What a real backend demands of the harness

Two assumptions that hold for an in-process fake do not survive contact with a
server:

- **A fake clock is not a clock.** `store_clock.advance(61)` is free against a
  `FakeClock` and impossible against `now()` on a server. Time is passed by
  `advance_store_time()`, which each backend implements the only way it can --
  by moving a fake clock, or by actually waiting.
- **An in-process seam does not exist across a socket.** Postgres has no
  `_staged` to park in, because its atomic step is a single SQL statement on the
  far side of a connection. `stepped()` returns `None` there, and that absence
  is *evidence* rather than an exemption: a store with no window has nowhere to
  be parked. The guard against abusing it lives in the stepper test.
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from ration.contract import Store
from ration.memory import MemoryStore
from ration.postgres import PostgresStore

try:
    from ration.duckdb_store import DuckDBStore
except ImportError:  # pragma: no cover - exercised by not having the driver
    DUCKDB_AVAILABLE = False
else:
    DUCKDB_AVAILABLE = True
from tests.support.clock import FakeClock
from tests.support.faults import FaultInjector
from tests.support.gate import Gate
from tests.violators import (
    ClientExpiryStore,
    ConflictWritesStore,
    CreateAlwaysStore,
    FailOpenStore,
    GlobalCounterStore,
    NaivePostgresStore,
    NaiveStore,
    RecyclingStore,
    StaleClockStore,
    TokenDroppingStore,
)

__all__ = [
    "CASES",
    "DUCKDB_AVAILABLE",
    "POSTGRES_DSN",
    "REQUIRE_POSTGRES",
    "BackendUnderTest",
    "Case",
]


#: Where the suite expects a Postgres to be. Unset means one is not available
#: and the case is not registered.
POSTGRES_DSN = os.environ.get("RATION_POSTGRES_DSN", "").strip()

#: Set in CI, and the reason a missing backend cannot go quiet. Absence of a
#: DSN is convenient locally and is a **hole** in a gate: a service container
#: that failed to start, or an environment variable dropped from the workflow,
#: leaves a green run in which the only real store was never exercised.
#: `test_the_suite_discriminates.py` asserts the case is registered whenever
#: this is set.
REQUIRE_POSTGRES = bool(os.environ.get("RATION_REQUIRE_POSTGRES", "").strip())

#: How long the suite is prepared to actually wait when it cannot fake time.
#: Short enough that a full run stays quick; long enough to survive a round trip
#: to a server on a loaded CI runner.
REAL_TTL = 0.5


def _parking(cls: type[MemoryStore], gate: Gate) -> type[MemoryStore]:
    """Subclass `cls` so its staging seam parks in `gate`.

    Done here rather than by handing the store a hook, so that no production
    constructor grows a parameter existing only for a test.
    """
    return type(
        f"Parking{cls.__name__}",
        (cls,),
        {"_staged": lambda self: gate.arrive()},
    )


@dataclass
class BackendUnderTest:
    """A built store, plus the handles a conformance test needs to press on it.

    Everything here is a capability of the **harness**, not of the contract.
    None of it is required of a backend and none of it appears in `Store`.
    """

    case: Case
    store: Store
    #: Passes `seconds` of store time, by whatever means this backend has.
    advance_store_time: Callable[[float], None]
    #: Moves a clock on the *caller's* side of the boundary. For a store that
    #: never consults one, this is a no-op -- and that it then changes nothing
    #: is the result rather than a gap.
    skew_client_clock: Callable[[float], None]
    #: Makes the store unreachable, as close to genuinely as the backend allows.
    partition: Callable[[], Iterator[None]]
    #: Makes the next write raise `Ambiguous`, having done what `applied` says.
    ambiguity: Callable[..., Iterator[None]]
    #: `None` where the store's atomic step is not in this process.
    _stepped: Callable[[Gate], BackendUnderTest] | None
    _prefix: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    _keys: int = field(default=0, repr=False)

    @property
    def name(self) -> str:
        return self.case.name

    @property
    def supports_stepping(self) -> bool:
        return self._stepped is not None

    def key(self) -> str:
        """A key nothing else has touched, in this run or in a previous one.

        The prefix is per instance and random because a real backend keeps what
        the last run wrote. A suite that reuses key names against a persistent
        store is asserting about someone else's leftovers.
        """
        self._keys += 1
        return f"{self.case.name}/{self._prefix}/key-{self._keys}"

    def stepped(self, gate: Gate) -> BackendUnderTest | None:
        return self._stepped(gate) if self._stepped is not None else None


@dataclass(frozen=True)
class Case:
    """One implementation, and the properties it is expected to fail."""

    name: str
    violates: frozenset[str]
    build: Callable[..., BackendUnderTest]


def _memory_case(
    name: str,
    violates: set[str],
    store_class: type[MemoryStore],
    *,
    wrap: Callable[[Store], Store] | None = None,
    needs_client_clock: bool = False,
) -> Case:
    def build(gate: Gate | None = None) -> BackendUnderTest:
        store_clock = FakeClock()
        client_clock = FakeClock()
        cls = _parking(store_class, gate) if gate is not None else store_class
        kwargs: dict[str, object] = {"clock": store_clock}
        if needs_client_clock:
            kwargs["client_clock"] = client_clock
        faults = FaultInjector(cls(**kwargs))  # type: ignore[arg-type]
        store: Store = wrap(faults) if wrap is not None else faults
        return BackendUnderTest(
            case=case,
            store=store,
            advance_store_time=store_clock.advance,
            skew_client_clock=client_clock.advance,
            partition=faults.partition,
            ambiguity=faults.ambiguity,
            _stepped=lambda g: case.build(g),
        )

    case = Case(name=name, violates=frozenset(violates), build=build)
    return case


def _duckdb_case(name: str, violates: set[str]) -> Case:
    """A backend with no server, which is the whole of what makes it interesting."""

    def build(gate: Gate | None = None) -> BackendUnderTest:
        # In-process is not the same as having a seam. DuckDB runs inside this
        # interpreter, but its atomic step is a statement executed by the
        # engine, so there is no Python-level window to park in any more than
        # there is across a socket. Parking would have to happen inside DuckDB's
        # own transaction manager. Handing back a "stepped" instance would give
        # the test a gate that never fires.
        assert gate is None, "the DuckDB store's atomic step is inside the engine"

        directory = tempfile.mkdtemp(prefix="ration-duckdb-")
        address = {"path": os.path.join(directory, "ration.duckdb")}
        store = DuckDBStore(lambda: address["path"])
        store.ensure_schema()

        @contextmanager
        def partition() -> Iterator[None]:
            # A path under a directory that does not exist. DuckDB raises
            # IOException on open, which the backend translates. This is the
            # store genuinely failing to reach its substrate rather than the
            # harness refusing on its behalf -- the same standard the Postgres
            # case holds itself to with an unlistening port.
            was = address["path"]
            address["path"] = os.path.join(directory, "gone", "nowhere.duckdb")
            store.close()
            try:
                yield
            finally:
                address["path"] = was
                store.close()

        faults = FaultInjector(store)

        return BackendUnderTest(
            case=case,
            store=faults,
            # The engine reads its own clock, so time cannot be faked here any
            # more than it can against a server. This is the same honest cost
            # the Postgres case pays, and the reason REAL_TTL is small.
            advance_store_time=time.sleep,
            # DuckDB is never told what time the caller thinks it is. That the
            # assertion then holds trivially is the correct result.
            skew_client_clock=lambda _seconds: None,
            partition=partition,
            ambiguity=faults.ambiguity,
            _stepped=None,
        )

    case = Case(name=name, violates=frozenset(violates), build=build)
    return case


def _postgres_case(
    name: str, violates: set[str], store_class: type[PostgresStore]
) -> Case:
    """The backends that are not fakes, and therefore the ones testing this file."""

    def build(gate: Gate | None = None) -> BackendUnderTest:
        # A store whose atomic step is a SQL statement has no seam to park in.
        # Handing back a "stepped" instance anyway would be worse than handing
        # back nothing: the gate would never fire, and the test would pass on a
        # window it never entered.
        assert gate is None, "the Postgres store has no in-process seam"

        # The address is swapped, not intercepted. `partition()` here means the
        # store is pointed somewhere that refuses a connection, so what the
        # suite observes is Postgres failing and the backend translating it --
        # rather than a refusal the harness performed on the store's behalf.
        address = {"dsn": POSTGRES_DSN}
        store = store_class(lambda: address["dsn"])
        store.ensure_schema()

        @contextmanager
        def partition() -> Iterator[None]:
            # Port 1 is reserved and nothing listens on it. A DSN that failed to
            # *parse* would raise a different error and prove something else.
            was = address["dsn"]
            address["dsn"] = "postgresql://ration@127.0.0.1:1/ration"
            try:
                yield
            finally:
                address["dsn"] = was

        # Ambiguity stays simulated for every backend, including this one. The
        # write really does reach Postgres and really does commit; what is
        # injected is the loss of the answer, which is the half a caller cannot
        # arrange and the half the property is about.
        faults = FaultInjector(store)

        return BackendUnderTest(
            case=case,
            store=faults,
            # The only honest implementation against a server's clock, and
            # the reason `REAL_TTL` is a fraction of a second rather than the
            # minute a fake clock skips for free.
            advance_store_time=time.sleep,
            # Postgres is never told what time the caller thinks it is, so
            # there is no clock here to skew. That the assertion then holds
            # trivially is the correct result, not a weakened one.
            skew_client_clock=lambda _seconds: None,
            partition=partition,
            ambiguity=faults.ambiguity,
            _stepped=None,
        )

    case = Case(name=name, violates=frozenset(violates), build=build)
    return case


CASES: list[Case] = [
    _memory_case("correct", set(), MemoryStore),
    _memory_case("create-always", {"compare-and-set"}, CreateAlwaysStore),
    _memory_case(
        "conflict-writes", {"compare-and-set", "atomicity"}, ConflictWritesStore
    ),
    _memory_case("naive", {"atomicity"}, NaiveStore),
    _memory_case("recycling", {"version-monotonic"}, RecyclingStore),
    _memory_case("global-counter", {"version-monotonic"}, GlobalCounterStore),
    _memory_case(
        "client-expiry",
        {"store-evaluated-expiry"},
        ClientExpiryStore,
        needs_client_clock=True,
    ),
    _memory_case("stale-clock", {"store-evaluated-expiry"}, StaleClockStore),
    # A correct store behind a layer that swallows the refusal, because that is
    # where failing open is actually written.
    _memory_case("fail-open", {"unreachable-refuses"}, MemoryStore, wrap=FailOpenStore),
    _memory_case("token-dropping", {"ambiguity-resolvable"}, TokenDroppingStore),
]

if DUCKDB_AVAILABLE:
    # Registered unconditionally where Postgres needs a DSN, and that
    # asymmetry is the first thing this backend demonstrates: there is no
    # server to name, because the store is a file this process opens. It is
    # also, exactly, why it cannot be a coordination store shared between
    # processes -- `tests/test_duckdb_single_process.py` asserts the consequence.
    CASES.append(_duckdb_case("duckdb", set()))

if POSTGRES_DSN:
    CASES.append(_postgres_case("postgres", set(), PostgresStore))
    # The violator that matters most. Without it the suite would only ever
    # reject fakes this repository wrote, and a contract that cannot
    # discriminate between *real* candidates has bought nothing.
    CASES.append(
        _postgres_case("postgres-naive", {"atomicity"}, NaivePostgresStore)
    )
