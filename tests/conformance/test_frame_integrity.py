"""Frame property 2: a bit flipped anywhere in the frame is refused as corruption.

Two of the frame's hostile cases -- a bit flipped in the body, a bit flipped in
the header -- and the failure the check exists to prevent: a corrupted frame
reaching a payload decoder. The header half is what a payload-level check
reused as the frame's would have left bare (`body-only-crc`). Every frame here is decoded against a ledger that has
seen nothing, so the sequence check cannot catch what the CRC missed: a
flipped sequence bit either refuses as corruption or reaches the decoder, and
there is no third outcome to hide behind. Violated by `unchecked-crc` and by
`body-only-crc`.
"""

from __future__ import annotations

import pytest

from ration.frame import CRC_MISMATCH, HEADER_SIZE, Frame, FrameRefusal, Sequence
from tests.support.codecs import BODY, CodecUnderTest

#: Header offsets, from the layout in `ration.frame`'s docstring.
KIND = 1
EPOCH_LOW_BYTE = 5
COUNT_LOW_BYTE = 9


@pytest.mark.conformance("frame-integrity")
def test_a_flipped_bit_anywhere_is_refused_as_corruption(codec: CodecUnderTest) -> None:
    healthy = codec.healthy(Sequence(7, 3))

    # In the body. The failure the check prevents, verbatim: with no check,
    # this reaches the payload decoder as the body.
    refused = codec.decode(codec.flip_bit(healthy, HEADER_SIZE + 5, bit=2))
    assert isinstance(refused, FrameRefusal), (
        f"a corrupted body reached the payload decoder: {refused!r}"
    )
    assert refused.control == CRC_MISMATCH, refused
    assert isinstance(codec.decode(healthy), Frame)

    # In the header's sequence field, count word. Against a fresh ledger any
    # count is the baseline, so a reader without a header check hands this
    # frame on with a sequence its sender never sent -- and takes it as the
    # position every later frame from that sender is judged against.
    refused = codec.decode(codec.flip_bit(healthy, COUNT_LOW_BYTE, bit=3))
    assert isinstance(refused, FrameRefusal), (
        f"a frame with a corrupted sequence reached the decoder: {refused!r}"
    )
    assert refused.control == CRC_MISMATCH, refused
    assert isinstance(codec.decode(healthy), Frame)

    # Epoch word, same argument, worse consequence: a corrupted epoch above
    # the sender's real one makes every honest frame after it a replay.
    refused = codec.decode(codec.flip_bit(healthy, EPOCH_LOW_BYTE, bit=3))
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == CRC_MISMATCH, refused
    assert isinstance(codec.decode(healthy), Frame)

    # In the kind byte -- the field that says which decoder to use, which a
    # body-only check would have left unchecked. Every reader
    # refuses this frame because the flipped kind is unregistered; what the
    # property decides is that it is refused for being CORRUPT, because a
    # flip that landed on a registered kind would select the wrong decoder
    # and no kind check would ever notice.
    refused = codec.decode(codec.flip_bit(healthy, KIND, bit=1))
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == CRC_MISMATCH, (
        "a flipped bit in the header was refused for what the flipped value "
        f"happened to mean rather than for being corrupt: {refused}"
    )
    accepted = codec.decode(healthy)
    assert isinstance(accepted, Frame)
    assert accepted.body == BODY
