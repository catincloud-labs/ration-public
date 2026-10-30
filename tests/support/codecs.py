"""Every frame codec the suite runs against, and what each is expected to fail.

The sibling of `backends.py`, and the same argument applies: the suite's job
is to **discriminate between readers**, so this registry is its subject -- one
codec expected to pass every property, and a set that each drop exactly one
check. The frame suite asks for a known-negative that fails without the check
beside a known-positive that passes with it; the known-negative is a row here, and strict xfail is what makes its failure a requirement rather than
an observation.

What a case hands a test is a codec plus the means to build frames wrong on
purpose *from that codec's own healthy frames*. That last part matters for the
one violator whose defect is in both directions: `body-only-crc` computes its
CRC over the body alone when writing and when reading, so a hostile frame
forged with the correct CRC rule would fail it for a reason it does not
claim. Forgery therefore goes through the case's own checksum, and a violator
is measured only against the check it dropped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ration.frame import (
    _CHECKED_HEADER,
    _HEADER,
    FINGERPRINT,
    FRAME_VERSION,
    HEADER_SIZE,
    ROLE_CLAIM,
    Frame,
    FrameCodec,
    FrameRefusal,
    Sequence,
    SequenceLedger,
)
from tests.frame_violators import (
    BodyOnlyCrcCodec,
    CounterOnlyCodec,
    CrcBeforeVersionCodec,
    CollapsesLayersCodec,
    DropsDanglingTextCodec,
    IgnoresTrailingCodec,
    LenientTextCodec,
    LibraryStrictTextCodec,
    NonCanonicalTextCodec,
    SequencesBeforeCheckingCodec,
    SkipsTextDecodeCodec,
    UncheckedCrcCodec,
    UncheckedKindCodec,
    UncheckedLengthCodec,
    UncheckedSequenceCodec,
    UncheckedVersionCodec,
)

__all__ = ["CODECS", "BODY", "CLAIM", "CodecCase", "CodecUnderTest"]

#: A body in the shape a frame carries: canonical JSON, which this suite never
#: decodes. Its content is irrelevant to every check and its length is not --
#: long enough that a flipped bit has somewhere to land that is not the header.
BODY = b'{"payload":{"layers":{"models":"9f2c"},"models":["a","b"]},"payload_kind":"fingerprint"}'

#: A claim-only body, the shape `role-claim` names, which this suite never
#: decodes either. The version is arbitrary; the length is chosen: it makes a
#: 41-byte frame, which is not a multiple of three, so the frame's text ends in
#: a character with unused bits -- the bits a non-canonical text sets.
CLAIM = b'{"role_version":123456}'


@dataclass
class CodecUnderTest:
    """A built codec, plus the forge a hostile-frame test needs."""

    case: CodecCase
    codec: FrameCodec
    _ledgers: dict[str, SequenceLedger] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.case.name

    def ledger(self, sender: str = "sender") -> SequenceLedger:
        """The receiver's ledger for `sender`, keyed here because the frame carries no sender."""
        return self._ledgers.setdefault(sender, SequenceLedger())

    def healthy(self, sequence: Sequence, body: bytes = BODY) -> bytes:
        return self.codec.encode(FINGERPRINT, sequence, body)

    def healthy_text(self, sequence: Sequence, body: bytes = CLAIM) -> str:
        """A claim frame in the text form a request header carries."""
        return self.codec.encode_text(ROLE_CLAIM, sequence, body)

    def decode_text(self, text: str) -> Frame | FrameRefusal:
        """Decode `text` against a ledger that has seen nothing, for the reason `decode` gives."""
        return self.codec.decode_text(text, SequenceLedger())

    def decode(self, data: bytes, sender: str | None = None) -> Frame | FrameRefusal:
        """Decode `data`; with no `sender`, against a ledger that has seen nothing.

        That default is how the properties stay separate. All but one
        are not about sequence, and a test for one of them that carried
        sequence state between frames would let a reader that consumes a
        sequence on refusal -- `sequences-before-checking` -- fail the
        properties it does not claim, beside the one it does. A violator
        that fails everything proves nothing about
        which check caught it, so each property is decided with the others
        held still, and the one that is about the ledger names its sender.
        """
        ledger = SequenceLedger() if sender is None else self.ledger(sender)
        return self.codec.decode(data, ledger)

    def forge(
        self,
        frame: bytes,
        *,
        version: int | None = None,
        kind: int | None = None,
        sequence: Sequence | None = None,
        length: int | None = None,
        body: bytes | None = None,
        recompute_crc: bool = True,
    ) -> bytes:
        """`frame` with the named fields rewritten, and the CRC made honest again.

        The header is rebuilt with the overrides, `body` replaces the body,
        and the CRC is then recomputed under the case's own rule so the forged
        frame carries a CRC a sender using that rule would have written. Pass `recompute_crc=False` to keep the
        original CRC, which is what a frame corrupted after it was checksummed
        looks like.
        """
        v, k, value, ln, crc = _HEADER.unpack_from(frame)
        new_body = frame[HEADER_SIZE:] if body is None else body
        if version is not None:
            v = version
        if kind is not None:
            k = kind
        if sequence is not None:
            value = sequence.value
        ln = len(new_body) if body is not None else ln
        if length is not None:
            ln = length
        checked = _HEADER.pack(v, k, value, ln, 0)[:_CHECKED_HEADER]
        if recompute_crc:
            crc = self.codec.checksum(checked, new_body[:ln])
        return checked + crc.to_bytes(4, "big") + new_body

    @staticmethod
    def flip_bit(frame: bytes, offset: int, bit: int = 0) -> bytes:
        """`frame` with one bit flipped: the shape of corruption in transit."""
        mutable = bytearray(frame)
        mutable[offset] ^= 1 << bit
        return bytes(mutable)


@dataclass(frozen=True)
class CodecCase:
    """One codec, and the properties it is expected to fail."""

    name: str
    violates: frozenset[str]
    build: Callable[[], CodecUnderTest]


def _case(name: str, violates: set[str], codec_class: type[FrameCodec]) -> CodecCase:
    def build() -> CodecUnderTest:
        return CodecUnderTest(case=case, codec=codec_class())

    case = CodecCase(name=name, violates=frozenset(violates), build=build)
    return case


assert FRAME_VERSION == 1, "the forge above packs v1's layout; a new version re-derives it"

CODECS: list[CodecCase] = [
    _case("correct", set(), FrameCodec),
    _case("unchecked-length", {"frame-length"}, UncheckedLengthCodec),
    _case("ignores-trailing", {"frame-length"}, IgnoresTrailingCodec),
    _case("unchecked-crc", {"frame-integrity"}, UncheckedCrcCodec),
    # A payload-level check reused as the frame's, as a codec: a check that
    # covers the body and leaves the header bare.
    _case("body-only-crc", {"frame-integrity"}, BodyOnlyCrcCodec),
    _case("unchecked-version", {"frame-version"}, UncheckedVersionCodec),
    _case("crc-before-version", {"frame-version"}, CrcBeforeVersionCodec),
    _case("unchecked-kind", {"frame-kind"}, UncheckedKindCodec),
    _case("unchecked-sequence", {"frame-sequence"}, UncheckedSequenceCodec),
    _case("counter-only", {"frame-sequence"}, CounterOnlyCodec),
    _case(
        "sequences-before-checking",
        {"frame-sequence"},
        SequencesBeforeCheckingCodec,
    ),
    _case("lenient-text", {"frame-text"}, LenientTextCodec),
    _case("library-strict", {"frame-text"}, LibraryStrictTextCodec),
    _case("skips-text-decode", {"frame-text"}, SkipsTextDecodeCodec),
    # Every character checked against base64url, and a last character's
    # unused bits never looked at.
    _case("accepts-non-canonical", {"frame-text"}, NonCanonicalTextCodec),
    _case("drops-dangling-character", {"frame-text"}, DropsDanglingTextCodec),
    _case("collapses-layers", {"frame-text"}, CollapsesLayersCodec),
]
