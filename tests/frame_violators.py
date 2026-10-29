"""Frame codecs that are wrong on purpose, one check at a time.

The same standing as `violators.py`, for the same reason: a frame check whose
violator is missing looks exactly like a check whose violator silently passes.
The frame suite holds every hostile case to a known-negative that fails
without the check and a known-positive that passes with it -- and the
known-negative is not a bad frame, it is a **reader without the check**,
because that is the thing the suite has to be shown rejecting.

Each class removes exactly one check, and each is the shape a shortcut takes.
"HTTP already frames the request" removes the length check. "The CRC costs a
pass over the body" removes the integrity check. "The header is tiny, hash the
body" leaves the header out of the integrity check. "Requests are idempotent"
removes the sequence check. A 32-bit counter is what a sender restart rule
looks like when nobody wrote one. None of them is contrived; each is what the
tree would contain if the check had been left for later.

Where a violator must break two things to break one, it says so and declares
both in `tests/support/codecs.py`.
"""

from __future__ import annotations

import zlib

from ration.frame import (
    _CHECKED_HEADER,
    _HEADER,
    HEADER_SIZE,
    SEQUENCE_DUPLICATE,
    SEQUENCE_GAP,
    SEQUENCE_REPLAYED,
    Frame,
    FrameCodec,
    FrameRefusal,
    Sequence,
    SequenceLedger,
)

__all__ = [
    "BodyOnlyCrcCodec",
    "CrcBeforeVersionCodec",
    "CounterOnlyCodec",
    "IgnoresTrailingCodec",
    "SequencesBeforeCheckingCodec",
    "UncheckedCrcCodec",
    "UncheckedKindCodec",
    "UncheckedLengthCodec",
    "UncheckedSequenceCodec",
    "UncheckedVersionCodec",
]


class UncheckedLengthCodec(FrameCodec):
    """Violates: `frame-length`. Trusts the transport to have delivered a whole frame.

    Reads whatever is there. A header shorter than eighteen bytes is not a
    refusal but an exception out of `struct`, which is the very failure the
    check exists to prevent: bytes off a network treated as programmer error. A body shorter than
    the header declares is handed on to the CRC, which refuses it -- under the
    wrong name, so a log record cannot tell a cut frame from a corrupted one.
    """

    def header_present(self, data: bytes) -> FrameRefusal | None:
        return None

    def body_present(self, length: int, present: int) -> FrameRefusal | None:
        return None


class IgnoresTrailingCodec(FrameCodec):
    """Violates: `frame-length`. Takes the declared body and discards the rest.

    The natural reading of a length field: it says how much to take, so take
    that much. What it discards is either bytes no sender put in a frame, or
    -- from a sender that honestly checksummed a length shorter than what it
    sent -- bytes the sender did put there. Either way the frame this decodes
    is one nobody built, and nothing said so.
    """

    def nothing_trailing(self, length: int, present: int) -> FrameRefusal | None:
        return None


class UncheckedCrcCodec(FrameCodec):
    """Violates: `frame-integrity`. Carries the CRC and never compares it.

    The failure the check exists to prevent, verbatim: a corrupted frame
    reaches a payload decoder. With
    this reader a flipped bit in the body arrives as the body, and a flipped
    bit in the sequence field arrives as a sequence the sender never sent --
    accepted outright by a ledger that has seen nothing, or refused by one
    that remembers under the sequence check's name, as though the sender had
    skipped a frame.
    """

    def integrity(
        self, checked_header: bytes, body: bytes, crc: int
    ) -> FrameRefusal | None:
        return None


class BodyOnlyCrcCodec(FrameCodec):
    """Violates: `frame-integrity`. Checks the body and leaves the header bare.

    The shortcut of reusing a payload-level check as the frame's, written
    down as a codec. Both directions agree, so
    every healthy frame passes and every corrupted body is caught; what is
    left uncovered is the version, kind, sequence and length, which is to say
    every field that decides what happens to the body.
    """

    def checksum(self, checked_header: bytes, body: bytes) -> int:
        return zlib.crc32(body) & 0xFFFFFFFF


class UncheckedVersionCodec(FrameCodec):
    """Violates: `frame-version`. Decodes every version as the one it knows."""

    def version_known(self, version: int) -> FrameRefusal | None:
        return None


class CrcBeforeVersionCodec(FrameCodec):
    """Violates: `frame-version`. Checks integrity first, then asks which version it read.

    Each check is present and each is correct; only the order is wrong, and
    the order is the contract a future version leans on. A v2 frame that moves
    the CRC is refused here as corrupted rather than as a version this reader
    does not speak, so the one refusal that would tell an operator to upgrade
    is the one this reader cannot produce.
    """

    def decode(self, data: bytes, ledger: SequenceLedger) -> Frame | FrameRefusal:
        data = bytes(data)
        refused = self.header_present(data)
        if refused is not None:
            return refused
        version, kind, value, length, crc = _HEADER.unpack_from(data)
        present = len(data) - HEADER_SIZE
        refused = self.body_present(length, present)
        if refused is not None:
            return refused
        body = data[HEADER_SIZE : HEADER_SIZE + length]
        # The one line that moved: integrity before version.
        refused = self.integrity(data[:_CHECKED_HEADER], body, crc)
        if refused is not None:
            return refused
        refused = self.version_known(version)
        if refused is not None:
            return refused
        refused = self.nothing_trailing(length, present)
        if refused is not None:
            return refused
        refused = self.kind_known(kind)
        if refused is not None:
            return refused
        sequence = Sequence.from_value(value)
        refused = self.in_sequence(sequence, ledger)
        if refused is not None:
            return refused
        ledger.advance(sequence)
        return Frame(kind=kind, sequence=sequence, body=body)


class UncheckedKindCodec(FrameCodec):
    """Violates: `frame-kind`. Hands any kind on, for the payload decoder to find out."""

    def kind_known(self, kind: int) -> FrameRefusal | None:
        return None


class UncheckedSequenceCodec(FrameCodec):
    """Violates: `frame-sequence`. Never consults the ledger.

    The reader HTTP alone gives you: each request is decoded on its own
    merits, so a retried `POST` is served twice and a replayed one is served
    late.
    """

    def in_sequence(
        self, sequence: Sequence, ledger: SequenceLedger
    ) -> FrameRefusal | None:
        return None


class CounterOnlyCodec(FrameCodec):
    """Violates: `frame-sequence`. A 32-bit counter, and no rule for a restart.

    What a sequence check looks like when nobody wrote the restart rule: the
    epoch word is carried and ignored. A sender that restarts at a fresh epoch
    and count zero is refused as a replay of count zero, so the honest restart
    is the one this reader turns away -- and a stalled older instance whose
    count happens to be next is the one it lets in.
    """

    def in_sequence(
        self, sequence: Sequence, ledger: SequenceLedger
    ) -> FrameRefusal | None:
        last = ledger.last
        if last is None or sequence.count == last.count + 1:
            return None
        if sequence.count == last.count:
            control = SEQUENCE_DUPLICATE
        elif sequence.count < last.count:
            control = SEQUENCE_REPLAYED
        else:
            control = SEQUENCE_GAP
        return FrameRefusal(control, f"count {sequence.count} after {last.count}")


class SequencesBeforeCheckingCodec(FrameCodec):
    """Violates: `frame-sequence`. Records the sequence as soon as it can read it.

    Every check is present and the ledger's rule is correct; the ledger just
    moves before the CRC has spoken. A frame corrupted in transit therefore
    consumes its sequence, and the sender's retry of the very same frame --
    intact this time -- is refused as a duplicate of a frame nothing accepted.
    """

    def decode(self, data: bytes, ledger: SequenceLedger) -> Frame | FrameRefusal:
        data = bytes(data)
        refused = self.header_present(data)
        if refused is not None:
            return refused
        version, kind, value, length, crc = _HEADER.unpack_from(data)
        # The defect: judged and recorded here, before anything else is known.
        sequence = Sequence.from_value(value)
        refused = self.in_sequence(sequence, ledger)
        if refused is not None:
            return refused
        ledger.advance(sequence)
        refused = self.version_known(version)
        if refused is not None:
            return refused
        present = len(data) - HEADER_SIZE
        refused = self.body_present(length, present)
        if refused is not None:
            return refused
        body = data[HEADER_SIZE : HEADER_SIZE + length]
        refused = self.integrity(data[:_CHECKED_HEADER], body, crc)
        if refused is not None:
            return refused
        refused = self.nothing_trailing(length, present)
        if refused is not None:
            return refused
        refused = self.kind_known(kind)
        if refused is not None:
            return refused
        return Frame(kind=kind, sequence=sequence, body=body)
