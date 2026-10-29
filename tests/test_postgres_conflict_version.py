"""What `Conflict.version` actually carries, pinned against a real server.

Not a conformance test, and deliberately not: it declares no contract property,
because the contract requires a conflict to be *reported without writing* and
says nothing about the version it carries. Adding a property here would add a
guarantee before a consumer needs one, and would narrow the field of backends
for a number nobody reads.

**It exists because the natural reading of a conflict is wrong here.** A
conflict reads as reporting the version the key had moved to. Under
concurrency `PostgresStore` reports the version the caller came *from*: the
statement that decides the outcome and the statement that reports the version
share one snapshot, and only the `UPDATE` half gets `EvalPlanQual`. Docstrings
can agree with each other about that and disagree with the server, and this
test is where the server is asked.

The interleaving is **forced, not raced for.** A second connection holds the row
lock in an open transaction, so there is no window to miss and no false pass to
rate -- which is the distinction `test_atomicity.py` draws between its stepper
and its hammer, applied here to a question the stepper cannot reach.
"""

from __future__ import annotations

import threading
import uuid

import psycopg
import pytest

from ration.contract import ABSENT, Applied, Conflict, Record
from ration.postgres import PostgresStore
from tests.support.backends import POSTGRES_DSN

pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN, reason="no Postgres configured; RATION_POSTGRES_DSN is unset"
)

#: Long enough that the contending thread has certainly reached the lock and
#: blocked. It is not a race window -- the blocker holds its transaction open
#: until this test commits it, so waiting longer changes nothing and waiting too
#: little would fail loudly on the join below rather than pass quietly.
_REACH_THE_LOCK = 1.0


def _blocked(fn) -> threading.Thread:
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
    threading.Event().wait(_REACH_THE_LOCK)
    return thread


@pytest.fixture
def postgres_store() -> PostgresStore:
    store = PostgresStore(POSTGRES_DSN)
    store.ensure_schema()
    return store


@pytest.fixture
def unique_key(request: pytest.FixtureRequest) -> str:
    """A key nothing else has touched, here or in a previous run.

    Same reasoning as `BackendUnderTest.key`: a real backend keeps what the last
    run wrote, so a fixed name asserts about someone else's leftovers.
    """
    return f"conflict-version/{uuid.uuid4().hex[:8]}/{request.node.name}"


def test_a_losing_update_reports_the_version_it_came_from(
    postgres_store: PostgresStore, unique_key: str
) -> None:
    seed = postgres_store.compare_and_set(unique_key, ABSENT, Record(b"seed", "seed"))
    assert isinstance(seed, Applied)

    holder = psycopg.connect(POSTGRES_DSN)
    try:
        holder.execute(
            "UPDATE ration_keys SET version = version + 1, payload = %s, token = %s"
            " WHERE key = %s AND version = %s",
            (b"winner", "winner", unique_key, seed.version),
        )

        outcome: dict[str, object] = {}
        thread = _blocked(
            lambda: outcome.update(
                result=postgres_store.compare_and_set(
                    unique_key, seed.version, Record(b"loser", "loser")
                )
            )
        )
        holder.commit()
        thread.join(timeout=30.0)
        assert not thread.is_alive(), "the losing writer never returned"
    finally:
        holder.close()

    result = outcome["result"]
    current = postgres_store.read(unique_key)

    # The part the contract requires, and the reason this is not a defect.
    assert isinstance(result, Conflict), "the loser must not have applied"
    assert current.record is not None
    assert current.record.token == "winner", "the loser wrote anyway"

    # The part that is merely true. If this ever starts reporting `current`,
    # the store got better and `Conflict`'s docstring is now too weak -- which
    # is a finding, not a failure, and this assertion is where it surfaces.
    assert result.version == seed.version
    assert current.version == seed.version + 1
    assert result.version != current.version


def test_a_losing_create_reports_absence(
    postgres_store: PostgresStore, unique_key: str
) -> None:
    holder = psycopg.connect(POSTGRES_DSN)
    try:
        holder.execute(
            "INSERT INTO ration_keys (key, version, payload, token)"
            " VALUES (%s, 1, %s, %s)",
            (unique_key, b"winner", "winner"),
        )

        outcome: dict[str, object] = {}
        thread = _blocked(
            lambda: outcome.update(
                result=postgres_store.compare_and_set(
                    unique_key, ABSENT, Record(b"loser", "loser")
                )
            )
        )
        holder.commit()
        thread.join(timeout=30.0)
        assert not thread.is_alive(), "the losing creator never returned"
    finally:
        holder.close()

    result = outcome["result"]
    current = postgres_store.read(unique_key)

    assert isinstance(result, Conflict), "the loser must not have applied"
    assert current.record is not None
    assert current.record.token == "winner", "the loser wrote anyway"

    # `ON CONFLICT DO NOTHING` yields no row, so the fallback reads a snapshot in
    # which the key does not exist yet: absence, which is a version.
    assert result.version == ABSENT
    assert current.version == 1
