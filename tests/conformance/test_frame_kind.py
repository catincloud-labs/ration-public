"""Frame property 4: a payload kind the registry does not name is refused.

The case of a payload kind unknown to the reader. The kind is the field that
selects a decoder, and a reader that hands an unknown one on is asking the
payload layer to find out -- which is the frame not being checked first, by
another name. Violated by `unchecked-kind`.

The property is ONE test function, and the registry sweep below lives inside
it rather than beside it. That is not tidiness: as a second `frame-kind`
function, the sweep would be XPASS(strict) for `unchecked-kind` -- a reader
that never checks the kind decodes every REGISTERED kind perfectly well, so it
passes the arm it does not violate while failing the arm it does. `AGENTS.md`
names that shape ("splitting a property into tests that different violators
fail differently").
"""

from __future__ import annotations

import pytest

from ration.frame import (
    KINDS,
    UNKNOWN_PAYLOAD_KIND,
    Frame,
    FrameRefusal,
    Sequence,
)
from tests.support.codecs import CodecUnderTest


@pytest.mark.conformance("frame-kind")
def test_an_unregistered_kind_is_refused(codec: CodecUnderTest) -> None:
    healthy = codec.healthy(Sequence.start(7))

    unknown = max(KINDS) + 100
    assert unknown not in KINDS
    refused = codec.decode(codec.forge(healthy, kind=unknown))
    assert isinstance(refused, FrameRefusal), (
        f"a kind the registry does not name was handed on for decoding: {refused!r}"
    )
    assert refused.control == UNKNOWN_PAYLOAD_KIND, refused
    accepted = codec.decode(healthy)
    assert isinstance(accepted, Frame)
    assert accepted.kind_name == "fingerprint"

    # Zero is not "no kind"; it is a kind nothing registered.
    assert 0 not in KINDS
    refused = codec.decode(codec.forge(healthy, kind=0))
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == UNKNOWN_PAYLOAD_KIND, refused
    assert isinstance(codec.decode(healthy), Frame)

    # The other half of "a code the registry names": EVERY row round-trips
    # under its own name, not just the one the fixture happens to encode.
    # Vacuous for a registry of a single row; not once a second code exists,
    # and a registry whose codes are never exercised is a list of names
    # nothing reports a verdict on.
    for code, name in KINDS.items():
        decoded = codec.decode(codec.forge(healthy, kind=code))
        assert isinstance(decoded, Frame), (
            f"kind {code} ({name}) is in the registry and was refused: {decoded!r}"
        )
        assert decoded.kind == code, decoded
        assert decoded.kind_name == name, decoded
