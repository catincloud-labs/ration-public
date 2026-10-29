"""Frame property 5: per sender, exactly the next sequence, or a fresh epoch at zero.

The frame's duplicated-sequence and skipped-sequence cases, plus the rule for
what a receiver does with a sender that restarts. Under HTTP a retried `POST`
is a duplicate the transport delivers happily, and that retry is the first
thing below. The last thing
below is its mirror: a retry of a frame that was *refused* has not been seen,
and must be accepted. The two retries are byte-identical to HTTP; the ledger
is what tells them apart. This is the one property decided against a ledger
that remembers, so it names its sender on every decode. Violated by
`unchecked-sequence`, by `counter-only`, which has no restart rule, and by
`sequences-before-checking`, which has the right rule and applies it too
early.
"""

from __future__ import annotations

import pytest

from ration.frame import (
    SEQUENCE_DUPLICATE,
    SEQUENCE_GAP,
    SEQUENCE_REPLAYED,
    Frame,
    FrameRefusal,
    Sequence,
)
from tests.support.codecs import CodecUnderTest

SENDER = "one-sender"


@pytest.mark.conformance("frame-sequence")
def test_exactly_the_next_sequence_or_a_fresh_epoch_at_zero(codec: CodecUnderTest) -> None:
    def decode(frame: bytes) -> Frame | FrameRefusal:
        return codec.decode(frame, sender=SENDER)

    first = codec.healthy(Sequence(7, 0))
    assert isinstance(decode(first), Frame)

    # The retried POST: the same frame, delivered twice.
    refused = decode(first)
    assert isinstance(refused, FrameRefusal), (
        f"a frame already accepted was accepted again: {refused!r}"
    )
    assert refused.control == SEQUENCE_DUPLICATE, refused
    assert isinstance(decode(codec.healthy(Sequence(7, 1))), Frame)

    # Skipped: a frame this receiver never saw sits between.
    refused = decode(codec.healthy(Sequence(7, 3)))
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == SEQUENCE_GAP, refused
    assert isinstance(decode(codec.healthy(Sequence(7, 2))), Frame)

    # Behind: a sequence the receiver has moved past, and not the last one.
    refused = decode(codec.healthy(Sequence(7, 1)))
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == SEQUENCE_REPLAYED, refused
    assert isinstance(decode(codec.healthy(Sequence(7, 3))), Frame)

    # The sender restarts. Its epoch is a lease version, so it is strictly
    # above the last one and need not be adjacent; its count starts again.
    restarted = decode(codec.healthy(Sequence(9, 0)))
    assert isinstance(restarted, Frame), (
        f"an honest restart at a fresh epoch was refused: {restarted}"
    )

    # The stalled older instance, still counting in the epoch it lost.
    refused = decode(codec.healthy(Sequence(7, 4)))
    assert isinstance(refused, FrameRefusal), (
        f"a sender replaying an epoch the receiver has left was accepted: {refused!r}"
    )
    assert refused.control == SEQUENCE_REPLAYED, refused
    assert isinstance(decode(codec.healthy(Sequence(9, 1))), Frame)

    # A new epoch that does not start at zero has skipped frames too.
    refused = decode(codec.healthy(Sequence(10, 5)))
    assert isinstance(refused, FrameRefusal), refused
    assert refused.control == SEQUENCE_GAP, refused
    assert isinstance(decode(codec.healthy(Sequence(9, 2))), Frame)

    # A refused frame has not consumed its sequence. This one arrives cut
    # short -- a shape every reader refuses, whichever check it is missing,
    # so the only thing decided here is what the refusal did to the ledger.
    # The sender's retry is the same frame, whole, and it is the FIRST time
    # the receiver has accepted it.
    healthy = codec.healthy(Sequence(9, 3))
    assert isinstance(decode(healthy[:-4]), FrameRefusal)
    retried = decode(healthy)
    assert isinstance(retried, Frame), (
        "the retry of a frame nothing accepted was refused, so the refused "
        f"frame consumed the sequence the ledger should never have moved to: {retried}"
    )
    assert retried.sequence == Sequence(9, 3)
