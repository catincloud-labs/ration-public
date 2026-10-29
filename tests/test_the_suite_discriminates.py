"""The suite's own conformance check: can it reject, and does it know what it tests?

`AGENTS.md` states this repository's hardest rule -- a conformance suite that
cannot reject is not a suite, and every property must have an implementation
that violates it and is expected to fail. Everything else in `tests/` enforces
that rule for the contract. This file enforces it for the suite, because a rule
that lives only in a document is a rule someone has to remember.

The individual "can it reject" answers are given elsewhere, by strict xfail: a
violator that passes its own property is a failing test. What is left over is
the bookkeeping that strict xfail cannot see -- a property nothing violates, and
a violator whose property no longer exists.
"""

from __future__ import annotations

import pytest

from tests.support.backends import CASES, REQUIRE_POSTGRES, Case
from tests.support.codecs import CODECS, CodecCase
from tests.support.declared import DECLARED_BY_FIXTURE, DECLARED_PROPERTIES, UNDECLARED

#: The two registries, by the fixture that runs each. The frame suite is held
#: to the rule above exactly as the store suite is, and by the same assertions
#: rather than a second copy of them.
Registry = list[Case] | list[CodecCase]
REGISTRIES: dict[str, Registry] = {"backend": CASES, "codec": CODECS}


def _claimed(registry: Registry) -> set[str]:
    return {prop for case in registry for prop in case.violates}


def _declared(fixture: str) -> set[str]:
    return DECLARED_BY_FIXTURE.get(fixture, set())


def test_every_conformance_test_declares_the_property_it_decides() -> None:
    assert UNDECLARED == [], (
        "these tests run against every implementation without saying which "
        f"property they decide, so no violator is expected to fail them: "
        f"{UNDECLARED}"
    )


@pytest.mark.parametrize("fixture", sorted(REGISTRIES))
def test_every_property_the_suite_tests_has_a_violator(fixture: str) -> None:
    orphans = _declared(fixture) - _claimed(REGISTRIES[fixture])
    assert orphans == set(), (
        f"{sorted(orphans)} are checked by tests that nothing is expected to "
        "fail. A property with no violator is indistinguishable from one whose "
        "violator silently passes, and neither is being tested."
    )


@pytest.mark.parametrize("fixture", sorted(REGISTRIES))
def test_every_violator_names_a_property_the_suite_actually_tests(fixture: str) -> None:
    phantoms = _claimed(REGISTRIES[fixture]) - _declared(fixture)
    assert phantoms == set(), (
        f"{sorted(phantoms)} are declared violated by some implementation, but "
        "no test that runs it decides them. A renamed property retires its "
        "violator in silence, which looks exactly like everything working."
    )


def test_the_two_registries_do_not_share_a_property_name() -> None:
    """A property is decided by tests that run one registry, never both.

    The bookkeeping above is per registry, so a name used in both would let a
    store violator be "covered" by a frame test that never runs a store.
    """
    shared = _declared("backend") & _declared("codec")
    assert shared == set(), f"{sorted(shared)} are declared by tests of both kinds"
    assert DECLARED_PROPERTIES == _declared("backend") | _declared("codec")


@pytest.mark.parametrize("fixture", sorted(REGISTRIES))
def test_the_suite_is_run_against_something_expected_to_pass(fixture: str) -> None:
    """A suite that fails everything is as uninformative as one that passes everything.

    Rejection is only evidence if something is not rejected, so at least one
    registered implementation must be expected to survive the whole contract.
    """
    clean = [case.name for case in REGISTRIES[fixture] if not case.violates]
    assert clean, (
        "every registered implementation is expected to fail something, so a "
        "run in which they all fail proves nothing about any of them"
    )


def test_a_real_backend_is_present_where_one_is_required() -> None:
    """In CI, a missing real backend must be red rather than quiet.

    Every other implementation in the registry is a fake this repository wrote,
    including the one expected to pass. A run of fakes proves the suite
    discriminates and proves nothing about any store, so the one case that is
    not a fake cannot be allowed to disappear silently -- which is exactly what
    a service container failing to start, or `RATION_POSTGRES_DSN` being dropped
    from the workflow, would otherwise do.

    Locally the requirement is off and the Postgres case is simply absent, which
    is why this assertion exists rather than a hard-coded expectation.
    """
    if not REQUIRE_POSTGRES:
        return
    names = [case.name for case in CASES]
    assert "postgres" in names, (
        "RATION_REQUIRE_POSTGRES is set, so a real backend was expected in the "
        f"registry and only these were built: {names}. A green run without it "
        "is a run of this repository's own fakes."
    )


@pytest.mark.parametrize("fixture", sorted(REGISTRIES))
def test_no_property_is_claimed_by_a_case_that_is_otherwise_correct(fixture: str) -> None:
    """The registry says what it means, in both directions.

    A case with an empty `violates` is a claim that it satisfies the entire
    contract. There is no third state, and this is the assertion that stops one
    being introduced by a blank field.
    """
    for case in REGISTRIES[fixture]:
        assert isinstance(case.violates, frozenset), case.name
        assert all(prop for prop in case.violates), (
            f"{case.name} declares an empty property name, which matches no "
            "test and therefore silences its own xfail"
        )
