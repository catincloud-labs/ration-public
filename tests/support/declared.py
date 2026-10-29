"""What collection observed, kept somewhere both the plugin and a test can read.

Deliberately a plain module rather than state on `conftest`. pytest imports
conftest under a name of its own choosing, so a test that did
`from tests.conftest import ...` could bind a second, empty copy and assert
happily against nothing -- which is the exact failure this file exists to catch
elsewhere.
"""

from __future__ import annotations

__all__ = ["DECLARED_BY_FIXTURE", "DECLARED_PROPERTIES", "UNDECLARED"]

#: Every property some conformance test declares it decides.
DECLARED_PROPERTIES: set[str] = set()

#: The same, split by the fixture -- and so the registry -- that ran the test.
#: A store violator claiming a frame property would be "tested" by the union
#: and by nothing that ever runs a store; the split is what catches it.
DECLARED_BY_FIXTURE: dict[str, set[str]] = {}

#: Conformance tests found with no property declared. Such a test gets no xfail
#: treatment, so every violator would be required to *pass* it -- the failure
#: mode hardest to notice by reading a green run.
UNDECLARED: list[str] = []
