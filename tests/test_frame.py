"""The frame codec on its own terms: the layout, the posture, and what it is not.

The conformance modules decide whether a codec *refuses* what it should. This
file decides the rest: that the bytes are the bytes the module's layout table
describes, that decode never raises on anything, that encode refuses a sender's
own mistakes loudly, and that the frame's refusals cannot be mistaken for the
envelope's. The last is a way the frame's design would be shown wrong, so it is
pinned as an assertion rather than left to the log reader.
"""

from __future__ import annotations

import base64
import random
import string
import zlib

import pytest

from ration.frame import (
    CONTROLS,
    CRC_MISMATCH,
    FINGERPRINT,
    FRAME_VERSION,
    HEADER_SIZE,
    KINDS,
    MALFORMED_TEXT,
    ROLE_CLAIM,
    TRUNCATED,
    Frame,
    FrameRefusal,
    Sequence,
    SequenceLedger,
    decode,
    decode_text,
    encode,
    encode_text,
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


def test_the_text_form_is_base64url_without_padding() -> None:
    """The text of the frame pinned above, so a change to the form is a visible diff.

    Pinned against the standard library's own base64url, with the padding
    stripped, and then as a literal for the diff reader. The header-only frame
    is 24 characters, the floor of any frame's text.
    """
    body = b'{"a":1}'
    frame = encode(FINGERPRINT, Sequence(7, 3), body)
    text = encode_text(FINGERPRINT, Sequence(7, 3), body)
    assert text == base64.urlsafe_b64encode(frame).decode("ascii").rstrip("=")
    assert text == "AQEAAAAHAAAAAwAAAAfwbC0eeyJhIjoxfQ"
    assert len(encode_text(FINGERPRINT, Sequence(7, 3), b"")) == 24


def test_a_claim_round_trips_through_its_text() -> None:
    claim = b'{"role_version":315821}'
    text = encode_text(ROLE_CLAIM, Sequence(42, 9), claim)
    assert text.isascii() and "=" not in text
    decoded = decode_text(text, SequenceLedger())
    assert decoded == Frame(kind=ROLE_CLAIM, sequence=Sequence(42, 9), body=claim)
    assert decoded.kind_name == KINDS[ROLE_CLAIM] == "role-claim"


def test_a_fault_in_the_bytes_under_the_text_is_refused_by_the_frame() -> None:
    """The layer split, on the reference reader, by the control that owns each fault.

    The conformance property decides only that such a fault is not the text's,
    because which frame check refuses it varies with the checks a reader has.
    Here the reader has them all, so the control is pinned too.
    """
    frame = encode(ROLE_CLAIM, Sequence(7, 0), b'{"role_version":315821}')
    flipped = bytearray(frame)
    flipped[HEADER_SIZE + 5] ^= 1 << 2
    corrupted = base64.urlsafe_b64encode(bytes(flipped)).decode("ascii").rstrip("=")
    refused = decode_text(corrupted, SequenceLedger())
    assert isinstance(refused, FrameRefusal) and refused.control == CRC_MISMATCH, refused
    cut = base64.urlsafe_b64encode(frame[:-4]).decode("ascii").rstrip("=")
    refused = decode_text(cut, SequenceLedger())
    assert isinstance(refused, FrameRefusal) and refused.control == TRUNCATED, refused


def test_an_empty_text_is_an_empty_frame_and_so_truncated() -> None:
    """An empty string is the text of no bytes, which the frame then refuses."""
    refused = decode_text("", SequenceLedger())
    assert isinstance(refused, FrameRefusal) and refused.control == TRUNCATED, refused


def test_decode_text_raises_only_on_a_callers_wrong_type() -> None:
    with pytest.raises(TypeError):
        decode_text(encode(FINGERPRINT, Sequence(1, 0), b""), SequenceLedger())  # type: ignore[arg-type]  # the wrong type is the point


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


def test_decode_text_returns_on_anything_and_never_raises() -> None:
    """A header's text is data too. Every prefix, every substitution, every random string.

    The substitutions draw on the alphabet, its padding, the standard
    alphabet's two characters, whitespace, a character outside ASCII and a
    lone surrogate, which a `str` can hold and no encoding can write.
    """
    rng = random.Random(2026_10_30)
    healthy = encode_text(ROLE_CLAIM, Sequence(7, 0), b'{"role_version":315821}')
    pool = (
        string.ascii_letters + string.digits + "-_" + "=+/ \t\r\n!." + "\uff21\u00e9\ud800"
    )
    inputs: list[str] = [healthy[:n] for n in range(len(healthy) + 1)]
    for _ in range(2000):
        mutable = list(healthy)
        for _ in range(rng.randint(1, 4)):
            mutable[rng.randrange(len(mutable))] = rng.choice(pool)
        inputs.append("".join(mutable))
    for _ in range(2000):
        inputs.append("".join(rng.choice(pool) for _ in range(rng.randrange(0, 80))))

    for text in inputs:
        result = decode_text(text, SequenceLedger())
        assert isinstance(result, (Frame, FrameRefusal)), (text, result)
        if isinstance(result, FrameRefusal):
            assert result.control in CONTROLS, result
            assert result.reason.isascii(), result


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
    # The ninth: a fault in a header's text is the frame's text form failing,
    # not an envelope that failed to parse, and the names keep them apart.
    assert MALFORMED_TEXT in CONTROLS
    assert MALFORMED_TEXT not in ENVELOPE_CONTROLS
    assert len(CONTROLS) == 9


def test_the_frame_version_is_one_and_the_registry_names_five_kinds() -> None:
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
        5: "role-claim",
    }
    # A name is what a payload decoder switches on, so two codes sharing one is
    # the same ambiguity as two codes sharing a meaning. The literal above
    # would not catch it on its own if a row were both added and renamed.
    assert len(set(KINDS.values())) == len(KINDS)
