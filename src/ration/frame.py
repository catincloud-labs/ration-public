"""The frame: a checked carrier beneath claims of authority and new payloads between nodes.

The rule the frame serves: a payload that carries a claim of authority from
one node to another -- a fencing claim, a vote, a command one node sends
another -- rides inside a frame, and so does every new payload between nodes;
a payload already framed stays framed. The rule says where a payload belongs.
A consumer moves a route onto the frame in its own change, so a claim can
still cross outside a frame while that move is under way. The frame is
checked before any payload is decoded, and this repository owns the frame.
This module is that frame, and nothing else -- no consumer, no route, no
transport.

Two kinds of existing payload are outside that rule on purpose:

- **Records in the coordination store**, this package's
  :class:`~ration.addressing.Address` among them. Such a record can carry a
  claim, since its version is a fencing token -- an address's ``version``, a
  lease's, a publication's generation -- but it crosses between nodes through
  the store rather than as a message. Property 1 of the contract makes that
  version rise per key and never be reused
  (`tests/conformance/test_version.py`), so it already orders the record, and
  a frame's sequence beside it would be a second ordering of one record.
- **The telemetry an existing shipper sends between nodes**, which carries no
  claim, so the rule does not bring it in. Framing it anyway would need a
  sequence rule this module does not have: a receiver here accepts exactly the
  next count (`tests/conformance/test_frame_sequence.py`), so one lost batch
  would leave every later batch refused as a gap until the sender started a
  new epoch or the receiver a fresh ledger.

## What a frame is

A fixed binary header in the space-packet shape, around a body this module
never looks inside:

| offset | size | field | check |
| --- | --- | --- | --- |
| 0 | 1 | frame version | must be the version this reader speaks |
| 1 | 1 | payload kind | must be a kind the registry names |
| 2 | 8 | sequence | per sender: the next one, or a fresh epoch |
| 10 | 4 | body length | the bytes present must honour it exactly |
| 14 | 4 | CRC-32 | over the fourteen bytes above and the body |

Big-endian throughout, because a header is read by whichever host it lands
on. The body is the consumer's own envelope, and this module never looks
inside it.

## The integrity check is the frame's, not the payload's

This is the reason the CRC covers the **header**. A digest a consumer's
envelope carries is computed over its payload alone, so reusing it would leave
the frame's own fields unprotected -- and the field that says which decoder to
use would be the one field nothing checked. The two checks answer different
questions: the CRC asks whether the frame was corrupted in transit, a digest
asks whether the payload matches what its producer hashed. Their refusals
carry different controls so a log record can tell them apart, and
`tests/test_frame.py` pins that distinction.

## The checks run in a stated order, and the order is the contract

A later frame version leans on it:

1. **Enough bytes for a header**, or the frame is `truncated`.
2. **Version**, before anything that assumes v1's layout. A v2 frame that
   moves the CRC must refuse as `unknown-frame-version`, not as a CRC failure
   -- so a flipped version byte is indistinguishable from a v2 frame, and
   both refuse here. That is accepted: neither reaches a payload decoder.
3. **Enough bytes for the declared body**, or `truncated`. The reader cannot
   tell a length that lies upward from a body cut short; both are the same
   observation and carry the same control, with the reason saying which
   was seen.
4. **CRC-32** over the header's first fourteen bytes and the declared body,
   or `crc-mismatch`. Before the trailing-bytes check on purpose: a length
   corrupted *downward* leaves the declared span failing its CRC, and that
   frame was corrupted, not padded.
5. **No bytes beyond the declared body**, or `trailing-bytes`. HTTP hands
   the reader one request's body, so anything past the frame is not a
   second frame; it is garbage, and a reader that ignored it would decode a
   frame no sender built.
6. **Kind**, or `unknown-payload-kind`.
7. **Sequence**, against the sender's ledger -- `sequence-duplicate`,
   `sequence-replayed` or `sequence-gap`.

Only then is the frame handed back, and only then does the ledger move.

## The sequence, and the rule for a sender that restarts

This is the one frame check with a live failure mode under HTTP -- a retried
`POST` is a duplicate the transport delivers happily. The rule:

- The sequence is one 64-bit integer whose **high word is the sender's epoch**
  and whose low word counts frames within it. Ordering is plain integer
  ordering, and "replayed" means "below the last one the receiver accepted".
- Within an epoch the receiver accepts exactly the next count. The same
  count is `sequence-duplicate`; a lower one is `sequence-replayed`; a higher
  one is `sequence-gap`.
- **A sender that restarts starts a new epoch at count zero**, and the
  receiver accepts any epoch above the last one it saw, at count zero only.
  An epoch at or below the last one is `sequence-replayed`: that is a
  restarted sender reusing a counter the receiver has seen, or an older
  instance of the sender still talking -- the stalled holder the contract's
  fencing token exists to refuse, arriving on the wire.
- **Where the epoch comes from** is the sender's, and this package already has
  the right source: a lease version. Property 1 of the contract makes it
  monotonic per key and never reused, not even after deletion, so a sender
  that re-announces its role after a restart holds an epoch strictly above
  any it held before, without persisting anything. A sender with no lease
  needs its own never-reused source, and this module does not check where
  the number came from; it checks that the receiver never accepts one it has
  moved past.
- **The ledger advances on acceptance only.** A frame refused for any reason
  has not consumed its sequence, so a sender whose frame was corrupted in
  transit retries with the *same* sequence and is accepted, while a sender
  whose frame was accepted and who retries anyway is refused as the duplicate
  it is. Those two retries look identical to HTTP; the ledger is what tells
  them apart.

**The first frame a receiver sees from a sender is trusted for ordering.**
A ledger that has seen nothing accepts whatever arrives and takes it as the
baseline, because the alternative -- refuse until the sender restarts at a
fresh epoch -- would turn every receiver restart into a forced restart of
every sender. The cost is stated rather than hidden: a replay against a fresh
ledger is not caught, and whether a consumer persists its ledgers across its
own restarts is the consumer's decision, not this module's.

## What is deliberately absent

- **The sender's identity.** The header carries no sender field. Which
  sender a frame belongs to is the receiver's knowledge -- the connection, the
  route, the envelope's own self-description -- and the receiver keys its
  ledgers by it. Putting an identity in the header would make it a claim the
  frame cannot verify.
- **Any opinion about the body.** The reader returns the body's bytes and
  the kind that says what they decode as. What decodes them is the
  consumer's, exactly as `addressing.py` refuses to parse an endpoint.
- **Decode raising.** Bytes off a network are data, not programmer error.
  Every way they can be wrong is a :class:`FrameRefusal` with a control from
  the closed set below. Encode is the other posture: a sender's own arguments
  are its own data, and a wrong one raises.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "CONTROLS",
    "CRC_MISMATCH",
    "EXPLORE_CLUSTER",
    "EXPLORE_PROFILE",
    "EXPLORE_QUERY",
    "FINGERPRINT",
    "FRAME_VERSION",
    "Frame",
    "FrameCodec",
    "FrameRefusal",
    "HEADER_SIZE",
    "KINDS",
    "SEQUENCE_DUPLICATE",
    "SEQUENCE_GAP",
    "SEQUENCE_REPLAYED",
    "Sequence",
    "SequenceLedger",
    "TRAILING_BYTES",
    "TRUNCATED",
    "UNKNOWN_FRAME_VERSION",
    "UNKNOWN_PAYLOAD_KIND",
    "decode",
    "encode",
]

#: Bump ONLY when the header's own shape changes, and in the same commit as the
#: change. A new payload kind is a new row in `KINDS`, not a new version.
FRAME_VERSION = 1

#: The header, as the one authoritative statement of the layout: version, kind,
#: sequence, body length, CRC-32. Network byte order.
_HEADER = struct.Struct("!BBQII")
HEADER_SIZE = _HEADER.size
#: The span the CRC covers within the header: everything before the CRC itself.
_CHECKED_HEADER = HEADER_SIZE - 4

#: The payload kinds that have been assigned codes. Closed: a reader refuses a
#: code that is not here, and a code means one body shape everywhere the frame
#: is spoken, which is the whole reason the registry is central rather than per
#: consumer.
#:
#: The code and the decoder it selects must move together or two nodes will
#: disagree about what a body is, and the decoder is the consumer's, in the
#: consumer's own code, so no one commit can hold both. What carries them
#: together is the consumer's **pin**: a consumer that installs this package at
#: a commit, not a branch or a tag, sees a new row only when it moves that pin,
#: and consumers that share a kind move their pins as one act or disagree about
#: what a body is.
#:
#: That is also why a row lands HERE first and the decoder follows. A code
#: nobody sends is inert: nothing encodes it, so no reader is ever asked about
#: it. The opposite order is the one that breaks -- a consumer that selects a
#: decoder on a code this registry does not name has every framed request
#: refused as `unknown-payload-kind`, which is the closed registry working
#: exactly as designed, against its own consumer.
#:
#: `fingerprint` is a consumer's envelope kind, named by the envelope's own kind
#: string so that the frame's kind and the envelope's payload kind agree by
#: name. Whether they agree in a given frame is the payload decoder's question,
#: not this module's.
#:
#: The three `explore-*` codes are one code per body-carrying route of a
#: consumer's service, rather than one code for all three: the routes' bodies
#: have different shapes, and a code means one body shape.
FINGERPRINT = 1
EXPLORE_QUERY = 2
EXPLORE_PROFILE = 3
EXPLORE_CLUSTER = 4
KINDS = MappingProxyType(
    {
        FINGERPRINT: "fingerprint",
        EXPLORE_QUERY: "explore-query",
        EXPLORE_PROFILE: "explore-profile",
        EXPLORE_CLUSTER: "explore-cluster",
    }
)

# The kind field is one byte. A row that does not fit it is a layout change
# wearing a registry entry, so it is refused at import rather than left for
# `struct` to raise about at the first encode. A `raise`, not an `assert`:
# `python -O` strips asserts, and a guard that vanishes under a flag is one
# that goes quiet rather than red.
if any(not 0 < kind < 256 for kind in KINDS):
    raise ValueError("a kind code must fit one byte and not be zero")

# The closed control set a decode can refuse with. `tests/test_frame.py` holds
# it apart from a consumer envelope decoder's, because a CRC failure and a
# digest mismatch landing as one signal would hide which layer failed. The one
# name the two sets share is `unknown-payload-kind`, on purpose: it is the same
# question asked of the same field at two depths, and a reader that sees it
# knows to check which layer answered by the refusal's type.
TRUNCATED = "truncated"
UNKNOWN_FRAME_VERSION = "unknown-frame-version"
CRC_MISMATCH = "crc-mismatch"
TRAILING_BYTES = "trailing-bytes"
UNKNOWN_PAYLOAD_KIND = "unknown-payload-kind"
SEQUENCE_DUPLICATE = "sequence-duplicate"
SEQUENCE_REPLAYED = "sequence-replayed"
SEQUENCE_GAP = "sequence-gap"

CONTROLS = frozenset(
    {
        TRUNCATED,
        UNKNOWN_FRAME_VERSION,
        CRC_MISMATCH,
        TRAILING_BYTES,
        UNKNOWN_PAYLOAD_KIND,
        SEQUENCE_DUPLICATE,
        SEQUENCE_REPLAYED,
        SEQUENCE_GAP,
    }
)

_WORD = 1 << 32


@dataclass(frozen=True)
class FrameRefusal:
    """A decode that declines, saying why.

    ``control`` is machine-readable and drawn from :data:`CONTROLS`; ``reason``
    is for the human reading a log. The same shape a payload decoder's refusal
    can take -- a control and a reason -- and deliberately a separate type, so
    that a route logging both never has to guess which layer refused.
    """

    control: str
    reason: str


@dataclass(frozen=True, order=True)
class Sequence:
    """One position in a sender's stream: its epoch, and its count within it.

    Ordered as the 64-bit integer it packs to, so comparing two of these is
    comparing their wire values. ``epoch`` is the half a sender must never
    reuse across restarts; see the module docstring for where it comes from.
    """

    epoch: int
    count: int

    def __post_init__(self) -> None:
        for name, value in (("epoch", self.epoch), ("count", self.count)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an int, not {type(value).__name__}")
            if not 0 <= value < _WORD:
                raise ValueError(f"{name} {value} is outside 32 bits")

    @classmethod
    def start(cls, epoch: int) -> Sequence:
        """The first sequence of an epoch, which is what a restarted sender sends."""
        return cls(epoch, 0)

    @classmethod
    def from_value(cls, value: int) -> Sequence:
        return cls(value >> 32, value & (_WORD - 1))

    @property
    def value(self) -> int:
        return (self.epoch << 32) | self.count

    def following(self) -> Sequence:
        """The next frame's sequence in this epoch.

        Raises rather than wrapping when the count is exhausted: a wrap would
        make the 4,294,967,296th frame of an epoch look like a replay of its
        first, and the sender's remedy is a new epoch, which only it can mint.
        """
        if self.count == _WORD - 1:
            raise OverflowError(
                f"epoch {self.epoch} has issued every count; start a new epoch"
            )
        return Sequence(self.epoch, self.count + 1)


@dataclass(frozen=True)
class Frame:
    """A frame that passed every check. The body is bytes; what they mean is the kind's."""

    kind: int
    sequence: Sequence
    body: bytes

    @property
    def kind_name(self) -> str:
        return KINDS[self.kind]


class SequenceLedger:
    """What a receiver has accepted from ONE sender.

    Keyed by sender outside this class, because the frame carries no sender
    field (see the module docstring). ``judge`` decides and ``advance``
    records, as two steps rather than one, so the reader can decide before it
    has finished checking and record only once it has -- and so a reader that
    records first is a violator the suite can write.
    """

    def __init__(self) -> None:
        self.last: Sequence | None = None

    def judge(self, sequence: Sequence) -> str | None:
        """The control this sequence would be refused with, or ``None`` to accept."""
        last = self.last
        if last is None:
            # Nothing to compare against; this frame becomes the baseline. The
            # cost is stated in the module docstring.
            return None
        if sequence.epoch > last.epoch:
            if sequence.count == 0:
                return None
            return SEQUENCE_GAP
        if sequence.epoch < last.epoch:
            return SEQUENCE_REPLAYED
        if sequence.count == last.count + 1:
            return None
        if sequence.count == last.count:
            return SEQUENCE_DUPLICATE
        if sequence.count < last.count:
            return SEQUENCE_REPLAYED
        return SEQUENCE_GAP

    def advance(self, sequence: Sequence) -> None:
        self.last = sequence


class FrameCodec:
    """Frame v1 in both directions, with each check a named step in a stated order.

    A class rather than two functions so that the steps are visible as the
    order the module docstring states, and so that an implementation which
    skips one -- the shape every shortcut takes -- is a subclass overriding
    one method, which is what `tests/frame_violators.py` holds against the
    suite. Nothing here is a seam that exists only for a test: every method is
    a check the reader performs.
    """

    def checksum(self, checked_header: bytes, body: bytes) -> int:
        """CRC-32 over the header's first fourteen bytes and the body."""
        return zlib.crc32(body, zlib.crc32(checked_header)) & 0xFFFFFFFF

    # -- writing -------------------------------------------------------------

    def encode(self, kind: int, sequence: Sequence, body: bytes) -> bytes:
        """The frame's bytes. A sender's own arguments are its own data, so this raises."""
        if kind not in KINDS:
            raise ValueError(f"payload kind {kind!r} is not a kind the registry names")
        if not isinstance(sequence, Sequence):
            raise TypeError("sequence must be a Sequence")
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise TypeError("body must be bytes")
        body = bytes(body)
        if len(body) >= _WORD:
            raise ValueError("body length does not fit the header's 32-bit length")
        checked = _HEADER.pack(FRAME_VERSION, kind, sequence.value, len(body), 0)[
            :_CHECKED_HEADER
        ]
        return checked + struct.pack("!I", self.checksum(checked, body)) + body

    # -- reading, in the stated order ----------------------------------------

    def decode(self, data: bytes, ledger: SequenceLedger) -> Frame | FrameRefusal:
        """Bytes to frame, or a typed refusal -- never an exception."""
        data = bytes(data)
        refused = self.header_present(data)
        if refused is not None:
            return refused
        version, kind, value, length, crc = _HEADER.unpack_from(data)
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
        sequence = Sequence.from_value(value)
        refused = self.in_sequence(sequence, ledger)
        if refused is not None:
            return refused
        ledger.advance(sequence)
        return Frame(kind=kind, sequence=sequence, body=body)

    def header_present(self, data: bytes) -> FrameRefusal | None:
        if len(data) < HEADER_SIZE:
            return FrameRefusal(
                TRUNCATED,
                f"truncated mid-header: {len(data)} bytes present, "
                f"{HEADER_SIZE} needed for a header",
            )
        return None

    def version_known(self, version: int) -> FrameRefusal | None:
        if version != FRAME_VERSION:
            return FrameRefusal(
                UNKNOWN_FRAME_VERSION,
                f"frame version {version} is not the known version {FRAME_VERSION}",
            )
        return None

    def body_present(self, length: int, present: int) -> FrameRefusal | None:
        if present < length:
            return FrameRefusal(
                TRUNCATED,
                f"truncated mid-body: the header declares {length} body bytes "
                f"and {present} are present",
            )
        return None

    def integrity(
        self, checked_header: bytes, body: bytes, crc: int
    ) -> FrameRefusal | None:
        if self.checksum(checked_header, body) != crc:
            return FrameRefusal(
                CRC_MISMATCH,
                "the CRC-32 over header and body does not match the one carried",
            )
        return None

    def nothing_trailing(self, length: int, present: int) -> FrameRefusal | None:
        if present > length:
            return FrameRefusal(
                TRAILING_BYTES,
                f"{present - length} bytes follow the declared body of {length}",
            )
        return None

    def kind_known(self, kind: int) -> FrameRefusal | None:
        if kind not in KINDS:
            return FrameRefusal(
                UNKNOWN_PAYLOAD_KIND,
                f"payload kind {kind} is not a kind this reader decodes",
            )
        return None

    def in_sequence(
        self, sequence: Sequence, ledger: SequenceLedger
    ) -> FrameRefusal | None:
        control = ledger.judge(sequence)
        if control is None:
            return None
        last = ledger.last
        seen = "nothing" if last is None else f"{last.epoch}:{last.count}"
        return FrameRefusal(
            control, f"sequence {sequence.epoch}:{sequence.count} after {seen}"
        )


_V1 = FrameCodec()


def encode(kind: int, sequence: Sequence, body: bytes) -> bytes:
    """A v1 frame around `body`. See :meth:`FrameCodec.encode`."""
    return _V1.encode(kind, sequence, body)


def decode(data: bytes, ledger: SequenceLedger) -> Frame | FrameRefusal:
    """Bytes to a checked frame, or a typed refusal. See :meth:`FrameCodec.decode`."""
    return _V1.decode(data, ledger)
