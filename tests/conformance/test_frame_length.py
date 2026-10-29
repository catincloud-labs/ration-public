"""Frame property 1: the bytes present honour the declared length, exactly.

Four of the frame's hostile cases land here -- truncated mid-header, truncated
mid-body, a length larger than the bytes present, and a length smaller than
them -- and the reader can only tell two of those four apart. Fewer bytes
than the header promises is one observation whether the body was cut or the
length lied upward, so both carry `truncated` and the reason says which was
seen. More bytes than promised is the other observation, and it is a
refusal rather than a tolerance because HTTP hands the reader one request's
body: nothing past the frame is a second frame. Violated by `unchecked-length`
and by `ignores-trailing`.
"""

from __future__ import annotations

import pytest

from ration.frame import TRAILING_BYTES, TRUNCATED, Frame, FrameRefusal, Sequence
from tests.support.codecs import BODY, CodecUnderTest


@pytest.mark.conformance("frame-length")
def test_the_bytes_present_must_honour_the_declared_length(
    codec: CodecUnderTest,
) -> None:
    healthy = codec.healthy(Sequence.start(7))

    # Truncated mid-header: not even enough bytes to say what this is.
    refused = codec.decode(healthy[:10])
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == TRUNCATED, refused
    assert isinstance(codec.decode(healthy), Frame)

    # Truncated mid-body: the header is intact and promises more than arrived.
    refused = codec.decode(healthy[:-5])
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == TRUNCATED, (
        "a body cut short is a truncation, and a reader that names it anything "
        f"else is reporting the symptom it noticed last: {refused}"
    )
    assert isinstance(codec.decode(healthy), Frame)

    # A length that lies upward, with a CRC honest about the lie: the length
    # check has to decide this one on its own, before the CRC gets a look.
    refused = codec.decode(codec.forge(healthy, length=len(BODY) + 7))
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == TRUNCATED, refused
    assert isinstance(codec.decode(healthy), Frame)

    # Trailing garbage after a complete frame.
    refused = codec.decode(healthy + b"\x00garbage")
    assert isinstance(refused, FrameRefusal), (
        "bytes past the declared body were decoded away, so the frame handed "
        f"on is one no sender built: {refused}"
    )
    assert refused.control == TRAILING_BYTES, refused
    assert isinstance(codec.decode(healthy), Frame)

    # A length that lies downward, honestly checksummed over the shorter span:
    # the declared frame is whole and there are still bytes left over.
    refused = codec.decode(codec.forge(healthy, length=len(BODY) - 3))
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == TRAILING_BYTES, refused
    accepted = codec.decode(healthy)
    assert isinstance(accepted, Frame)
    assert accepted.body == BODY
