"""A role moves between interpreters, against a real store, and the old holder is refused.

This is not a conformance test and declares no property. It decides nothing the
contract states, so `tests/test_the_suite_discriminates.py` never counts it.

**Why it exists.** `tests/test_addressing.py` already proves a role can be
released and taken over, and that the generation moves when it does. Every one of
those tests runs in **one interpreter, sharing one store object**. Addressing
exists so that a role is not pinned to a host by accident, so its claim is about
a role moving between **hosts** -- and a role able to move between hosts that
never has is that claim untested. An in-process handoff is evidence about the
module, not about the claim.

**A process is not a host, and this is careful not to pretend otherwise.**
What it removes is the possibility that a handoff works only because both parties
share in-process state: each worker builds its own `PostgresStore` from nothing
but a DSN, which is what a second host would do. What it cannot reach is the
partition a real second host introduces. So **this narrows the gap between the
module and the claim without closing it.**

**The interesting assertion is the last one, not the handoff.** Anyone can
watch a key change owner. The claim `addressing.py` actually makes -- and the
reason DNS lost -- is that `Address.version` is a **fencing token**: a holder
that stalled past its expiry and resumed carries the version it was granted at,
and a store that has moved on refuses it. `renew`'s own docstring is precise
about when that fires, and this test is built on that precision rather than on
a timeout:

    expiry alone does not break the chain; another holder does.

So the stale holder here is refused **because someone else took the role**, not
because time passed -- and the test proves that by first showing a renew *after*
expiry with no competitor succeeding.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import uuid

import pytest

from ration.addressing import key_for, resolve
from ration.postgres import PostgresStore
from tests.support.backends import POSTGRES_DSN
from tests.support.crossprocess import (
    announce_role,
    renew_with_version,
    resolve_role,
)

pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN, reason="no Postgres configured; RATION_POSTGRES_DSN is unset"
)

#: Long enough to survive a spawn and a round trip on a loaded runner, short
#: enough that waiting it out twice does not dominate the suite.
ROLE_TTL = 1.5


@pytest.fixture
def role() -> str:
    """A role name nothing else has used, in this run or a previous one.

    The store is persistent, so a fixed name would be asserting about whatever
    the last run left behind.
    """
    PostgresStore(POSTGRES_DSN).ensure_schema()
    return f"producer-{uuid.uuid4().hex[:8]}"


def _run(fn, *args):
    """Run `fn` in a spawned interpreter and return (pid, verdict)."""
    ctx = mp.get_context("spawn")
    with ctx.Pool(1) as pool:
        return pool.apply(fn, args)


def _distinct(pid: int, what: str) -> None:
    """The harness before the store, every time.

    A `spawn` pool that silently ran its task in the parent produces exactly the
    green this file is supposed to earn, and nothing else in the result shows
    it. This repository's cross-process probe already applies this rule; a role
    handoff needs it more, not less, because sharing an interpreter is the one
    thing the test exists to rule out.
    """
    assert pid != os.getpid(), (
        f"{what} ran in the parent interpreter, so it measured nothing about a "
        f"separate process (pid {pid})"
    )


def test_a_role_announced_in_one_process_resolves_in_another(role: str) -> None:
    """The base case, and it is worth stating separately from the handoff."""
    pid_a, granted = _run(
        announce_role, POSTGRES_DSN, role, "node-a.example:8080", "node-a", ROLE_TTL
    )
    _distinct(pid_a, "the announcing worker")
    assert granted.startswith("GRANTED"), granted

    pid_b, seen = _run(resolve_role, POSTGRES_DSN, role)
    _distinct(pid_b, "the resolving worker")
    assert pid_b != pid_a, "announce and resolve ran in the same interpreter"
    assert seen.startswith("AT node-a.example:8080 node-a "), (
        f"a caller that never held the role could not find it: {seen}"
    )


def test_a_second_process_is_refused_while_the_role_is_held(role: str) -> None:
    """Exclusivity across the boundary. Without this the handoff proves nothing.

    A store that let anyone announce would 'hand over' beautifully and would
    have no role in it at all.
    """
    _, granted = _run(
        announce_role, POSTGRES_DSN, role, "node-a.example:8080", "node-a", 30.0
    )
    assert granted.startswith("GRANTED"), granted

    pid_c, refused = _run(
        announce_role, POSTGRES_DSN, role, "node-b.example:8080", "node-b", 30.0
    )
    _distinct(pid_c, "the second claimant")
    assert refused == "REFUSED", (
        f"a second process took a role that was still held: {refused}"
    )


def test_the_role_moves_and_the_stale_holder_is_fenced(role: str) -> None:
    """The whole point: a handoff between interpreters, then the old holder refused.

    Five steps, and the third is the control for the fifth.
    """
    # 1. node-a takes the role.
    _, granted = _run(
        announce_role, POSTGRES_DSN, role, "node-a.example:8080", "node-a", ROLE_TTL
    )
    assert granted.startswith("GRANTED"), granted
    first_version = int(granted.split()[1])

    # 2. Wait it out. The store evaluates expiry, so this is a real wait --
    #    there is no clock here to move, which is the contract's property 2
    #    costing what it costs.
    _wait_until_absent(role)

    # 3. THE CONTROL. Renewing a lapsed grant that nobody contested must
    #    SUCCEED. `renew`'s docstring says expiry alone does not break the
    #    chain, and if this failed, step 5 would prove only that time passed --
    #    which is the weaker claim a reader would mistake for fencing.
    pid_r, renewed = _run(
        renew_with_version, POSTGRES_DSN, role, "node-a", first_version, ROLE_TTL
    )
    _distinct(pid_r, "the renewing worker")
    assert renewed.startswith("RENEWED"), (
        "a lapsed but uncontested grant was refused, so this test cannot "
        f"distinguish fencing from expiry: {renewed}"
    )
    renewed_version = int(renewed.split()[1])

    # Let it lapse again so node-b can take it.
    _wait_until_absent(role)

    # 4. node-b takes the role from a third interpreter.
    pid_2, taken = _run(
        announce_role, POSTGRES_DSN, role, "node-b.example:9090", "node-b", 30.0
    )
    _distinct(pid_2, "the taking-over worker")
    assert taken.startswith("GRANTED"), f"the role could not be taken over: {taken}"
    second_version = int(taken.split()[1])
    assert second_version > renewed_version, (
        f"the generation did not advance on handoff: {renewed_version} -> "
        f"{second_version}. A cached name record cannot tell these apart, which "
        f"is the whole reason the address carries one"
    )

    # A caller that never held it now sees node-b, at the new generation.
    _, seen = _run(resolve_role, POSTGRES_DSN, role)
    assert seen == f"AT node-b.example:9090 node-b {second_version}", (
        f"a resolver did not follow the role to its new holder: {seen}"
    )

    # 5. THE FENCE. node-a resumes, still holding the version it was granted
    #    at, and is refused -- not because its TTL passed, which step 3 showed
    #    is survivable, but because someone else acquired.
    pid_stale, fenced = _run(
        renew_with_version, POSTGRES_DSN, role, "node-a", renewed_version, ROLE_TTL
    )
    _distinct(pid_stale, "the stale holder")
    assert fenced == "REFUSED", (
        "a holder that stalled through a handoff renewed its lease anyway, so "
        f"the role was held twice: {fenced}"
    )

    # And the fence changed nothing: node-b still holds it, at its version.
    final = resolve(PostgresStore(POSTGRES_DSN), role)
    assert final is not None and final.holder == "node-b", (
        f"the refused renew disturbed the live holder: {final}"
    )
    assert final.version == second_version, (
        f"the refused renew advanced the generation: {final.version} != "
        f"{second_version}. A conflict must be reported WITHOUT writing"
    )


def _wait_until_absent(role: str) -> None:
    """Wait for the role to lapse, and fail loudly rather than proceed if it does not.

    Polls the store rather than sleeping `ROLE_TTL` and assuming. A fixed
    sleep on a loaded runner is how a handoff test comes to assert against a
    role that is still held, and the failure would present as the store being
    wrong.
    """
    import time

    store = PostgresStore(POSTGRES_DSN)
    deadline = time.monotonic() + ROLE_TTL * 8
    while time.monotonic() < deadline:
        if store.read(key_for(role)).record is None:
            return
        time.sleep(0.1)
    raise AssertionError(
        f"{role} was still held after {ROLE_TTL * 8:.1f}s, so the store is not "
        "evaluating expiry and nothing after this point would mean what it says"
    )
