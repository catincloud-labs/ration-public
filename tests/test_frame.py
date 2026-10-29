"""The frame codec on its own terms: the layout, the posture, and what it is not.

The conformance modules decide whether a codec *refuses* what it should. This
file decides the rest: that the bytes are the bytes the module's layout table
describes, that decode never raises on anything, that encode refuses a sender's
own mistakes loudly, and that the frame's refusals cannot be mistaken for the
envelope's. The last is a way the frame's design would be shown wrong, so it is
pinned as an assertion rather than left to the log reader.
"""

from __future__ import annotations

import random
import zlib

import pytest

from ration.frame import (
    CONTROLS,
    FINGERPRINT,
    FRAME_VERSION,
    HEADER_SIZE,
    KINDS,
    Frame,
    FrameRefusal,
    Sequence,
    SequenceLedger,
    decode,
    encode,
)

#: A fixture: the closed control set of an envelope decoder that frames carry,
#: written out here because no consumer is a dependency of this package; and
#: held apart from the frame's own `CONTROLS` on purpose, because the assertion
#: below is about which names the two sets share.
ENVELOPE_CONTROLS = frozenset(
    {
        "unknown-schema-version",
        "unknown-payload-kind",
        "malformed-envelope",
        "digest-mismatch",
    }
)


# -- the layout ---------------------------------------------------------------


def test_the_header_is_eighteen_bytes_in_the_stated_layout() -> None:
    """The exact bytes of one frame, so a change to the layout is a visible diff.

    Version 1, kind 1, epoch 7, count 3, length 7, then the CRC, then the body.
    The fourteen checked bytes are written out by hand from the layout table
    in the module docstring, and the CRC is `zlib`'s over exactly those bytes
    and the body -- the layout is pinned independently of the codec, and the
    checksum is pinned to the library rather than to a number the codec once
    produced. If this fails and the module changed, bump the version there in
    the same commit.
    """
    body = b'{"a":1}'
    checked = bytes.fromhex(
        "01"  # frame version
        "01"  # payload kind: fingerprint
        "0000000700000003"  # sequence: epoch 7, count 3
        "00000007"  # body length
    )
    crc = zlib.crc32(checked + body).to_bytes(4, "big")
    assert HEADER_SIZE == 18 == len(checked) + len(crc)
    assert encode(FINGERPRINT, Sequence(7, 3), body) == checked + crc + body
    assert crc.hex() == "f06c2d1e"  # the value this layout produces, for the diff reader


def test_a_frame_round_trips() -> None:
    body = b'{"payload_kind":"fingerprint"}'
    frame = encode(FINGERPRINT, Sequence(42, 9), body)
    decoded = decode(frame, SequenceLedger())
    assert decoded == Frame(kind=FINGERPRINT, sequence=Sequence(42, 9), body=body)
    assert decoded.kind_name == KINDS[FINGERPRINT] == "fingerprint"


def test_a_zero_length_body_is_a_frame() -> None:
    """The shape the frame allows a codec to support for a bodiless route.

    Whether a given route takes it is the consumer's decision; that the codec
    can carry it is decided here, so that decision starts from a fact.
    """
    frame = encode(FINGERPRINT, Sequence.start(1), b"")
    assert len(frame) == HEADER_SIZE
    decoded = decode(frame, SequenceLedger())
    assert isinstance(decoded, Frame)
    assert decoded.body == b""


def test_the_sequence_orders_as_the_integer_it_packs_to() -> None:
    earlier, later = Sequence(7, 0xFFFF_FFFF), Sequence(8, 0)
    assert earlier < later
    assert earlier.value < later.value
    assert Sequence.from_value(later.value) == later
    assert Sequence.start(3) == Sequence(3, 0)
    assert Sequence(3, 4).following() == Sequence(3, 5)


def test_an_exhausted_epoch_refuses_to_wrap() -> None:
    with pytest.raises(OverflowError):
        Sequence(1, 0xFFFF_FFFF).following()


def test_a_fresh_ledger_takes_the_first_frame_as_its_baseline() -> None:
    """A stated cost, pinned so it stays a decision rather than becoming a surprise."""
    ledger = SequenceLedger()
    assert ledger.judge(Sequence(200, 17)) is None
    ledger.advance(Sequence(200, 17))
    assert ledger.judge(Sequence(200, 18)) is None
    assert ledger.judge(Sequence(200, 17)) == "sequence-duplicate"


# -- the postures -------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(dict(kind=99), id="unregistered-kind"),
        pytest.param(dict(sequence=7), id="bare-int-for-sequence"),
        pytest.param(dict(body="text"), id="str-for-body"),
    ],
)
def test_encode_raises_on_a_senders_own_mistake(bad: dict[str, object]) -> None:
    arguments: dict[str, object] = dict(kind=FINGERPRINT, sequence=Sequence(1, 0), body=b"")
    arguments.update(bad)
    with pytest.raises((TypeError, ValueError)):
        encode(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("epoch", "count"),
    [(-1, 0), (0, -1), (1 << 32, 0), (0, 1 << 32)],
)
def test_a_sequence_outside_its_words_is_refused_at_construction(epoch: int, count: int) -> None:
    with pytest.raises(ValueError):
        Sequence(epoch, count)


def test_decode_returns_on_anything_and_never_raises() -> None:
    """Bytes off a network are data. Every prefix, every mutation, every random string.

    Not a proof, a sweep: several thousand inputs across the shapes corruption
    takes, each of which must come back as a frame or as a refusal carrying a
    control from the closed set.
    """
    rng = random.Random(2026_09_01)
    healthy = encode(FINGERPRINT, Sequence(7, 3), b'{"payload":{"models":["a","b"]}}')
    inputs: list[bytes] = [healthy[:n] for n in range(len(healthy) + 1)]
    for _ in range(2000):
        mutable = bytearray(healthy)
        for _ in range(rng.randint(1, 4)):
            mutable[rng.randrange(len(mutable))] ^= 1 << rng.randrange(8)
        inputs.append(bytes(mutable))
    for _ in range(2000):
        inputs.append(rng.randbytes(rng.randrange(0, 64)))

    for data in inputs:
        result = decode(data, SequenceLedger())
        assert isinstance(result, (Frame, FrameRefusal)), (data, result)
        if isinstance(result, FrameRefusal):
            assert result.control in CONTROLS, result


# -- what it is not -----------------------------------------------------------


def test_the_frames_refusals_cannot_be_mistaken_for_the_envelopes() -> None:
    """The failure this guards: a CRC failure and a `digest-mismatch` landing as one signal.

    The two checks answer different questions, so their refusals carry
    different names -- with one deliberate exception. Both layers can refuse
    an unknown payload kind, and that is the same question asked of the same
    field at two depths: the frame's registry says whether a code selects a
    decoder, the envelope's says whether that decoder recognises the name.
    """
    shared = CONTROLS & ENVELOPE_CONTROLS
    assert shared == {"unknown-payload-kind"}, sorted(shared)
    assert "digest-mismatch" not in CONTROLS
    assert "crc-mismatch" in CONTROLS
    assert "crc-mismatch" not in ENVELOPE_CONTROLS


def test_the_frame_version_is_one_and_the_registry_names_four_kinds() -> None:
    """Readings, not rules: the registry written out by hand, where a reader trips on it.

    The literal below is the ONE place the registry is spelled out rather than
    computed, so a row added, dropped or renumbered anywhere is a diff here,
    and a code silently reused is a red test rather than a dict literal quietly
    keeping the last of two identical keys. It cannot make a consumer that adds
    a kind edit this line in the same commit: a consumer is another repository,
    and no commit spans two. Where a code and its decoder actually meet is each
    consumer's build, which pins the revision of this package it decodes with.
    """
    assert FRAME_VERSION == 1
    assert dict(KINDS) == {
        1: "fingerprint",
        2: "explore-query",
        3: "explore-profile",
        4: "explore-cluster",
    }
    # A name is what a payload decoder switches on, so two codes sharing one is
    # the same ambiguity as two codes sharing a meaning. The literal above
    # would not catch it on its own if a row were both added and renamed.
    assert len(set(KINDS.values())) == len(KINDS)
