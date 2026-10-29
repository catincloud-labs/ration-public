"""How the suite decides whether a run means anything.

Every conformance test runs against every registered implementation, and each
test declares the one contract property it decides. Where a registered store is
expected to fail that property, the test is marked `xfail(strict=True)` -- so a
violator that passes is reported as a **failure**, not as a pleasant surprise.

That inversion is the whole mechanism. Without it, a violator that has
stopped violating anything -- because the property was weakened, a fixture
drifted, or an assertion stopped reaching -- looks exactly like a suite that is
working.

There are two registries and one mechanism. A test that takes `backend` runs
against every store in `tests/support/backends.py`; a test that takes `codec`
runs against every frame codec in `tests/support/codecs.py`. The marker, the
xfail and the bookkeeping are the same for both, because the frame contract asks
of the frame suite exactly what the store contract asks of the store suite: a
known-negative seen failing beside a known-positive seen passing.
"""

from __future__ import annotations

import pytest

from tests.support.backends import CASES, BackendUnderTest, Case
from tests.support.codecs import CODECS, CodecCase, CodecUnderTest
from tests.support.declared import DECLARED_BY_FIXTURE, DECLARED_PROPERTIES, UNDECLARED

#: Each registry, by the fixture name that runs it.
_REGISTRIES = {"backend": CASES, "codec": CODECS}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "conformance(property): the contract property this test decides",
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    for fixture, registry in _REGISTRIES.items():
        if fixture in metafunc.fixturenames:
            metafunc.parametrize(
                fixture,
                registry,
                ids=[case.name for case in registry],
                indirect=True,
            )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        names = getattr(item, "fixturenames", ())
        for fixture in _REGISTRIES:
            if fixture not in names:
                continue
            marker = item.get_closest_marker("conformance")
            if marker is None or not marker.args:
                UNDECLARED.append(item.nodeid)
            else:
                DECLARED_PROPERTIES.add(marker.args[0])
                DECLARED_BY_FIXTURE.setdefault(fixture, set()).add(marker.args[0])


def _expect_failure_where_violated(
    request: pytest.FixtureRequest, case: Case | CodecCase
) -> None:
    marker = request.node.get_closest_marker("conformance")
    if marker is None or not marker.args:
        raise AssertionError(
            f"{request.node.nodeid} runs against every implementation but does "
            "not declare which property it decides, so no violator is expected "
            "to fail it. Add @pytest.mark.conformance(...)."
        )
    prop: str = marker.args[0]
    if prop in case.violates:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=(
                    f"{case.name} violates {prop} on purpose; if this passes, "
                    "the property is not being tested"
                ),
            )
        )


@pytest.fixture
def backend(request: pytest.FixtureRequest) -> BackendUnderTest:
    case: Case = request.param
    _expect_failure_where_violated(request, case)
    return case.build()


@pytest.fixture
def codec(request: pytest.FixtureRequest) -> CodecUnderTest:
    case: CodecCase = request.param
    _expect_failure_where_violated(request, case)
    return case.build()
