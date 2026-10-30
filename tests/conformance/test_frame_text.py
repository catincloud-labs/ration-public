"""Frame property 6: only a frame's canonical text is read, and any other string is refused as text.

The form a frame takes in a request header, for a route with no HTTP body. A
reader of it must refuse every string that is not the text form as
`malformed-text`, before any frame check, and must hand the bytes a
well-formed text holds to the frame's checks unchanged -- so a fault in the
text is the text's refusal, and a fault in the bytes is the frame's. Violated
by `lenient-text`, which reads with the standard library's defaults; by
`skips-text-decode`, which hands the header to the frame reader undecoded; and
by `accepts-non-canonical`, which checks every character and never the round
trip.

The frame here is a `role-claim` frame around a claim-only body, which is the
use the form exists for. Every text is decoded against a ledger that has seen
nothing, for the reason `tests/support/codecs.py` gives: the property is
decided with the sequence held still.
"""

from __future__ import annotations

import base64
import string

import pytest

from ration.frame import (
    CONTROLS,
    MALFORMED_TEXT,
    ROLE_CLAIM,
    Frame,
    FrameRefusal,
    Sequence,
)
from tests.support.codecs import CLAIM, CodecUnderTest

#: The text form's alphabet in its value order, so a character's index is the
#: six bits it stands for.
ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"

#: An offset inside the text, away from either end, where every bit a
#: character carries is a data bit.
INSIDE = 20

#: The sender's epoch, chosen for its bits rather than its value. Its low byte
#: is `0x3F`, the last byte of a three-byte group in the header, so six one-bits
#: sit on a character boundary and the text holds base64url's `_` -- in bytes
#: every reader writes alike. The CRC's characters differ by reader, and
#: `body-only-crc` writes one with no `-` or `_` in it.
EPOCH = 63


@pytest.mark.conformance("frame-text")
def test_only_the_canonical_text_is_read_as_a_frame(codec: CodecUnderTest) -> None:
    healthy = codec.healthy_text(Sequence.start(EPOCH))

    def refused_as_text(text: str, what: str) -> None:
        refused = codec.decode_text(text)
        assert isinstance(refused, FrameRefusal), (
            f"{what} was read as a frame no sender wrote: {refused!r}"
        )
        assert refused.control == MALFORMED_TEXT, (
            f"{what} was refused, but as a fault in the frame rather than in "
            f"the text: {refused}"
        )
        assert isinstance(codec.decode_text(healthy), Frame)

    # The leniency that makes this a property: a character outside the
    # alphabet, which a default base64 decode drops, returning the bytes the
    # text held before it was added.
    refused_as_text(
        healthy[:INSIDE] + "!" + healthy[INSIDE:], "a character outside the alphabet"
    )

    # Whitespace inside the text, which the same decode skips.
    refused_as_text(healthy[:INSIDE] + " " + healthy[INSIDE:], "a space inside the text")

    # A character outside ASCII altogether, in place of one inside the text.
    refused_as_text(
        healthy[:INSIDE] + "\uff21" + healthy[INSIDE + 1 :], "a character outside ASCII"
    )

    # Padding. The form writes none, so a text that carries it was not
    # written by the form's encoder.
    assert len(healthy) % 4, "a text whose length is a multiple of four takes no padding"
    refused_as_text(healthy + "=" * (-len(healthy) % 4), "a padded text")

    # The standard alphabet's `+` and `/` in place of base64url's `-` and `_`:
    # the same bytes under a second alphabet, which is a second text.
    assert healthy[4:8].endswith("_"), "the epoch's low byte no longer lands on `_`"
    standard = healthy.replace("-", "+").replace("_", "/")
    refused_as_text(standard, "the standard alphabet's text")

    # A length that holds no whole number of bytes.
    assert len(healthy[:-2]) % 4 == 1
    refused_as_text(healthy[:-2], "a text one character past a multiple of four")

    # A last character whose unused bits are set. It holds the SAME bytes as
    # the healthy text under the standard library's reading, strict or not,
    # which is why the check is a round trip and not a character scan.
    noncanonical = healthy[:-1] + ALPHABET[ALPHABET.index(healthy[-1]) | 1]
    assert noncanonical != healthy
    padding = "=" * (-len(healthy) % 4)
    assert base64.urlsafe_b64decode(noncanonical + padding) == base64.urlsafe_b64decode(
        healthy + padding
    ), "the two texts must hold one frame, or this arm decides nothing"
    refused_as_text(noncanonical, "a second text of the same frame")

    # The other half: a fault in the BYTES a well-formed text holds is not the
    # text's to refuse. Swapping the first character for another inside the
    # alphabet writes a frame version this reader does not speak. Which frame
    # check refuses it depends on which checks a reader has -- version, or
    # the CRC the swap also broke -- so only the layer is decided here, and
    # the frame's own properties decide the rest.
    assert healthy[0] == "A", "a v1 frame's text opens with the version byte's top bits"
    refused = codec.decode_text("B" + healthy[1:])
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control in CONTROLS - {MALFORMED_TEXT}, (
        "a well-formed text's bytes were refused as a fault in the text, so "
        f"the frame's own refusal never reached the log: {refused}"
    )

    accepted = codec.decode_text(healthy)
    assert isinstance(accepted, Frame), (
        f"the canonical text of a healthy frame was refused: {accepted}"
    )
    assert accepted.kind == ROLE_CLAIM
    assert accepted.kind_name == "role-claim"
    assert accepted.sequence == Sequence.start(EPOCH)
    assert accepted.body == CLAIM
