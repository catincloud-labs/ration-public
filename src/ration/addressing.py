"""Where a role is, answered by the lease that already says who holds it.

A role must not be pinned to a host by accident, and a caller that reaches a
service by hostname has done exactly that. So services address each other by
**role** and something resolves the role to a host. This module is that
something, and it is thin on purpose.

**It introduces no new state, which is why it lives here rather than in a
repository of its own.** The code is decomposed by state ownership -- the
substrate and the services share a repository because they own the same state
-- and the fact "who holds this role right now" is *already* lease state.
Addressing is a **reading** of it. The address is carried as the lease's own
payload, so a role is announced and held in one operation and there is nothing
to keep in step.

**The alternative was to publish the address into the network's name service
and let callers resolve by DNS.** It loses for a reason that is not new: the
lease already decides who holds a role, so a name record is a *second copy of
that fact*, and it goes stale in exactly the moment that matters -- a failover.
Two copies of a fact is one fact and one stale claim. It also has no fencing
token, which is the concrete form of the same objection: a cached name keeps
answering for a holder whose lease has moved on, and the caller cannot tell.

So the network is still what carries the packets. It is the transport; it is
not the register of who holds what. Those are different jobs and only one of
them is coordination.

**What this costs, stated because it is real.** Service addressing depends on
the coordination store being reachable. The store refuses under partition, so a
peer that cannot reach it could already take no lease; it now also cannot
resolve a role. That widens the blast radius of a failure the lease layer
already accepts rather than introducing a new one -- but it does widen it, and a
reader should be told that rather than left to notice.
"""

from __future__ import annotations

from dataclasses import dataclass

from ration.contract import Store
from ration.lease import Lease, acquire

__all__ = ["ROLE_PREFIX", "Address", "announce", "key_for", "resolve"]

#: Namespaced so a role can never collide with a key some other consumer of the
#: contract chose. The prefix is part of the wire format rather than a
#: convenience: two consumers agreeing on a key by accident is a bug that
#: presents as an unrelated service losing its lease.
ROLE_PREFIX = "role/"


@dataclass(frozen=True)
class Address:
    """Where a role is, and the generation that answer belongs to."""

    role: str
    #: Whatever the transport needs -- a host, a `host:port`, a URL. This module
    #: does not parse it, because the moment it does it has an opinion about the
    #: transport, and the constraint this module serves is about roles, not
    #: about protocols.
    endpoint: str
    #: The holder's identity, from the lease token.
    holder: str
    #: **The fencing token, and the reason this is a dataclass rather than a
    #: string.** It is the lease version the address was read at. A caller that
    #: carries it can be refused by a callee that has since seen a newer one,
    #: which is precisely what a cached name record cannot offer. An address
    #: without a generation is a guess with good manners.
    version: int


def key_for(role: str) -> str:
    """The store key a role's address lives at."""
    if not role:
        raise ValueError("a role needs a name")
    if role.startswith(ROLE_PREFIX):
        # Otherwise `key_for(key_for(r))` silently addresses a different role,
        # and the failure is a lookup that succeeds against the wrong thing.
        raise ValueError(f"role {role!r} is already a role key")
    return f"{ROLE_PREFIX}{role}"


def announce(
    store: Store,
    role: str,
    endpoint: str,
    holder: str,
    *,
    ttl: float,
) -> Lease | None:
    """Claim `role` and publish `endpoint` as where it is. `None` if held.

    **`ttl` is required, and that is the whole difference between this and a
    name record.** A lease with no expiry keeps answering for a holder that
    stopped existing, which is the stale-DNS failure arriving through the door
    this module was built to close. The lease layer permits `ttl=None` because
    not every claim is an address; an address is not one of those cases.

    Refusal is a `None`, not an exception, and `Unreachable` is deliberately
    **not** caught -- both inherited from `acquire`, and for its reasons. A
    store that cannot be reached must not read as a role that is free.
    """
    if ttl is None or ttl <= 0:
        raise ValueError("an address needs an expiry; see this function's docstring")
    if not endpoint:
        raise ValueError("a role needs somewhere to be")
    return acquire(
        store, key_for(role), holder, ttl=ttl, payload=endpoint.encode("utf-8")
    )


def resolve(store: Store, role: str) -> Address | None:
    """Where `role` is, or `None` if nobody currently holds it.

    **Every call reads the store. There is no cache here and adding one would
    reintroduce the defect this module exists to avoid** -- see the module
    docstring, and `tests/test_addressing.py`, where a deliberately caching
    resolver is required to answer for a role that has expired.

    Expiry is not evaluated here. The contract's property 2 puts it in the
    store, so an expired role arrives as an absent record and this function
    cannot accidentally disagree with the thing that granted the lease.
    """
    reading = store.read(key_for(role))
    if reading.record is None:
        return None
    return Address(
        role=role,
        endpoint=reading.record.payload.decode("utf-8"),
        holder=reading.record.token,
        version=reading.version,
    )
