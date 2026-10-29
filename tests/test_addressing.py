"""Role addressing, against every store that is not a violator.

Not conformance tests. Addressing states no new contract property -- it is a
*consumer* of the lease, and the lease is a consumer of the contract. What these
decide is whether resolving a role behaves, given a store that already passes.

**The negative control at the bottom is the reason the rest is worth
anything.** Most of this file asserts that an address *stops* answering, and a
test asserting the absence of something can stay green while looking at
nothing. So a deliberately caching resolver -- the
stale-name-record defect, written down -- is required to keep answering for a
role whose lease has expired. If it ever stops, the expiry assertions above it
are measuring the passage of time and not the store.
"""

from __future__ import annotations

import pytest

from ration.addressing import Address, announce, key_for, resolve
from ration.lease import release
from tests.support.backends import CASES, REAL_TTL, BackendUnderTest

CORRECT_CASES = [case for case in CASES if not case.violates]


@pytest.fixture(params=CORRECT_CASES, ids=[case.name for case in CORRECT_CASES])
def correct(request: pytest.FixtureRequest) -> BackendUnderTest:
    case = request.param
    return case.build()


# -- what a role is --------------------------------------------------------


def test_a_role_resolves_to_where_its_holder_said_it_was(
    correct: BackendUnderTest,
) -> None:
    role = correct.key()
    held = announce(correct.store, role, "node-a.example:8080", "producer", ttl=REAL_TTL)
    assert held is not None

    found = resolve(correct.store, role)
    assert found == Address(
        role=role, endpoint="node-a.example:8080", holder="producer", version=held.version
    )


def test_a_role_nobody_holds_resolves_to_nothing(correct: BackendUnderTest) -> None:
    assert resolve(correct.store, correct.key()) is None


def test_a_second_holder_cannot_announce_a_held_role(
    correct: BackendUnderTest,
) -> None:
    role = correct.key()
    assert announce(correct.store, role, "node-a.example:8080", "producer", ttl=REAL_TTL)

    assert (
        announce(correct.store, role, "node-b.example:8080", "impostor", ttl=REAL_TTL) is None
    ), "two hosts announced the same role, so a caller could reach either"

    found = resolve(correct.store, role)
    assert found is not None
    assert found.endpoint == "node-a.example:8080", "the loser's address was published anyway"


def test_a_released_role_stops_resolving_and_can_move(
    correct: BackendUnderTest,
) -> None:
    role = correct.key()
    held = announce(correct.store, role, "node-a.example:8080", "producer", ttl=REAL_TTL)
    assert held is not None
    assert release(correct.store, held) is True

    assert resolve(correct.store, role) is None
    assert announce(correct.store, role, "node-b.example:8080", "producer", ttl=REAL_TTL)
    moved = resolve(correct.store, role)
    assert moved is not None and moved.endpoint == "node-b.example:8080"


# -- expiry, which is the whole difference from a name record --------------


def test_an_expired_role_stops_resolving(correct: BackendUnderTest) -> None:
    """The property the module exists for.

    A holder that stops renewing stops being reachable. Nothing here evaluates
    a clock -- the contract's second property puts expiry in the store, so this
    is an assertion that the store's answer reaches the caller unaltered.
    """
    role = correct.key()
    assert announce(correct.store, role, "node-a.example:8080", "producer", ttl=REAL_TTL)
    assert resolve(correct.store, role) is not None

    correct.advance_store_time(REAL_TTL * 2)

    assert resolve(correct.store, role) is None, (
        "the holder's TTL passed and the role still resolves, so a dead host is "
        "still being handed to callers"
    )


def test_the_generation_moves_when_the_role_does(correct: BackendUnderTest) -> None:
    """The fencing token, which is what a cached name cannot offer."""
    role = correct.key()
    first = announce(correct.store, role, "node-a.example:8080", "producer", ttl=REAL_TTL)
    assert first is not None
    before = resolve(correct.store, role)

    correct.advance_store_time(REAL_TTL * 2)
    second = announce(correct.store, role, "node-b.example:8080", "producer-b", ttl=REAL_TTL)
    assert second is not None
    after = resolve(correct.store, role)

    assert before is not None and after is not None
    assert after.version > before.version, (
        "the role moved to another host at the same generation, so a caller "
        "holding the old address cannot be told it is stale"
    )


# -- the arguments this refuses --------------------------------------------


@pytest.mark.parametrize("ttl", [None, 0, -1.0])
def test_an_address_without_a_future_expiry_is_refused(
    correct: BackendUnderTest, ttl: float | None
) -> None:
    with pytest.raises(ValueError):
        announce(correct.store, correct.key(), "node-a.example:8080", "producer", ttl=ttl)  # type: ignore[arg-type]


def test_a_role_without_a_name_or_an_endpoint_is_refused(
    correct: BackendUnderTest,
) -> None:
    with pytest.raises(ValueError):
        announce(correct.store, "", "node-a.example:8080", "producer", ttl=REAL_TTL)
    with pytest.raises(ValueError):
        announce(correct.store, correct.key(), "", "producer", ttl=REAL_TTL)


def test_a_role_key_is_refused_as_a_role_name() -> None:
    """`key_for(key_for(r))` would address a different role and succeed at it."""
    with pytest.raises(ValueError):
        key_for(key_for("producer"))


# -- the negative control --------------------------------------------------


class _CachingResolver:
    """Violates: an address stops answering when its lease expires.

    This is the name-record defect written down -- resolve once, remember the
    answer, keep serving it. It is not exported and nothing may build on it.
    """

    def __init__(self, store: object) -> None:
        self._store = store
        self._cache: dict[str, Address | None] = {}

    def resolve(self, role: str) -> Address | None:
        if role not in self._cache:
            self._cache[role] = resolve(self._store, role)  # type: ignore[arg-type]
        return self._cache[role]


def test_a_caching_resolver_still_answers_for_an_expired_role(
    correct: BackendUnderTest,
) -> None:
    """Proves `test_an_expired_role_stops_resolving` is testing something.

    If this ever returns `None`, the expiry test above is not observing the
    store deciding anything -- it would pass against a resolver that had simply
    never been asked twice, and the property would be unmeasured while green.
    """
    role = correct.key()
    assert announce(correct.store, role, "node-a.example:8080", "producer", ttl=REAL_TTL)

    stale = _CachingResolver(correct.store)
    assert stale.resolve(role) is not None

    correct.advance_store_time(REAL_TTL * 2)

    assert resolve(correct.store, role) is None, "the real resolver should refuse"
    assert stale.resolve(role) is not None, (
        "the caching resolver stopped answering, so this control is not "
        "reproducing the defect it exists to reproduce and the expiry test "
        "above is asserting nothing"
    )
