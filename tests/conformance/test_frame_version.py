"""Frame property 3: a version the reader does not speak is refused as that, first.

The case of a frame version ahead of the reader, with the half that makes
it a property rather than a comparison: the version is checked **before**
anything that assumes this version's layout. A v2 frame that moves the CRC
must refuse as a version this reader does not speak, not as a corrupted v1
frame -- the first refusal tells an operator to upgrade, the second tells
them to look at the wire. Violated by `unchecked-version` and by
`crc-before-version`, which has every check and only the order wrong.
"""

from __future__ import annotations

import pytest

from ration.frame import FRAME_VERSION, UNKNOWN_FRAME_VERSION, Frame, FrameRefusal, Sequence
from tests.support.codecs import CodecUnderTest


@pytest.mark.conformance("frame-version")
def test_an_unknown_version_is_refused_before_its_layout_is_assumed(
    codec: CodecUnderTest,
) -> None:
    healthy = codec.healthy(Sequence.start(7))

    # A version ahead of the reader, otherwise well-formed under v1's layout
    # and honestly checksummed: the only thing wrong is the version.
    refused = codec.decode(codec.forge(healthy, version=FRAME_VERSION + 1))
    assert isinstance(refused, FrameRefusal), (
        f"a frame of a version this reader does not speak was decoded as v1: {refused!r}"
    )
    assert refused.control == UNKNOWN_FRAME_VERSION, refused
    assert isinstance(codec.decode(healthy), Frame)

    # A version ahead of the reader whose layout has moved: what sits in v1's
    # CRC slot is not a CRC over what v1 thinks it covers. Version first, or
    # the refusal names the wrong problem.
    refused = codec.decode(
        codec.forge(healthy, version=FRAME_VERSION + 1, recompute_crc=False)
    )
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == UNKNOWN_FRAME_VERSION, (
        "a frame of a later version was refused for failing THIS version's "
        f"integrity check, so the one refusal that says 'upgrade' cannot be produced: {refused}"
    )
    assert isinstance(codec.decode(healthy), Frame)

    # Version zero is not a version either; "unknown" means "not this one".
    refused = codec.decode(codec.forge(healthy, version=0))
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == UNKNOWN_FRAME_VERSION, refused
    assert isinstance(codec.decode(healthy), Frame)
