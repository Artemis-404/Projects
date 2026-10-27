"""Frame protocol tests (FR-04, NFR-08)."""

from __future__ import annotations

import contextlib
import random

import pytest

from protocol.frames import (
    HEADER_SIZE,
    MAX_FRAME_PAYLOAD,
    Frame,
    FrameParser,
    FrameType,
    ProtocolError,
    decode_frame,
    decode_frames,
    encode_frame,
    encode_frames,
    parse_ping,
    ping_payload,
)


@pytest.mark.parametrize("frame_type", list(FrameType))
@pytest.mark.parametrize("payload_len", [0, 1, 3, 255, 1024, MAX_FRAME_PAYLOAD])
def test_roundtrip(frame_type: FrameType, payload_len: int) -> None:
    payload = bytes((i * 7) % 256 for i in range(payload_len))
    encoded = encode_frame(Frame(frame_type, payload))
    assert len(encoded) == payload_len + HEADER_SIZE
    frame, offset = decode_frame(encoded)
    assert frame.type is frame_type
    assert frame.payload == payload
    assert offset == len(encoded)


def test_wire_layout_is_type_length_payload() -> None:
    encoded = encode_frame(Frame(FrameType.DATA, b"abc"))
    assert encoded == bytes([0x01, 0x00, 0x03]) + b"abc"


def test_unknown_type_rejected() -> None:
    with pytest.raises(ProtocolError):
        Frame(FrameType.parse(0x09))
    with pytest.raises(ProtocolError):
        decode_frame(bytes([0xEE, 0x00, 0x00]))


def test_oversize_frame_rejected() -> None:
    with pytest.raises(ProtocolError):
        Frame(FrameType.DATA, b"x" * (MAX_FRAME_PAYLOAD + 1))
    with pytest.raises(ProtocolError):
        encode_frame(Frame(FrameType.DATA, b"x" * (MAX_FRAME_PAYLOAD + 1)))
    # A length field claiming more than the limit is refused before allocation.
    with pytest.raises(ProtocolError):
        decode_frame(bytes([0x01]) + (MAX_FRAME_PAYLOAD + 1).to_bytes(2, "big"))


def test_truncated_input_rejected() -> None:
    with pytest.raises(ProtocolError):
        decode_frame(b"\x01\x00")
    with pytest.raises(ProtocolError):
        decode_frame(b"\x01\x00\x05abc")


def test_incremental_parser_handles_fragmentation() -> None:
    frames = [Frame(FrameType.DATA, b"a" * 10), Frame(FrameType.PING, b"{}")]
    stream = encode_frames(frames)
    parser = FrameParser()
    out = []
    for i in range(0, len(stream), 4):
        out.extend(parser.feed(stream[i : i + 4]))
    assert [f.type for f in out] == [FrameType.DATA, FrameType.PING]
    assert out[0].payload == b"a" * 10


def test_parser_rejects_bad_frame_immediately() -> None:
    parser = FrameParser()
    with pytest.raises(ProtocolError):
        parser.feed(bytes([0x7F, 0x00, 0x01, 0x00]))


def test_parser_buffer_limit() -> None:
    parser = FrameParser(max_buffered=16)
    with pytest.raises(ProtocolError):
        parser.feed(b"\x00" * 32)


def test_fuzz_random_bytes_never_escape_protocol_error() -> None:
    rng = random.Random(1234)
    for _ in range(2000):
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 64)))
        parser = FrameParser(max_buffered=4096)
        try:
            frames = parser.feed(blob)
        except ProtocolError:
            continue
        for frame in frames:
            assert frame.type in set(FrameType)
            assert len(frame.payload) <= MAX_FRAME_PAYLOAD


def test_fuzz_mutations_of_valid_streams() -> None:
    rng = random.Random(99)
    base = encode_frames(
        [Frame(FrameType.DATA, b"hello" * 10), Frame(FrameType.PADDING, b"\x00" * 5)]
    )
    for _ in range(500):
        mutated = bytearray(base)
        for _ in range(rng.randrange(1, 5)):
            mutated[rng.randrange(len(mutated))] = rng.randrange(256)
        with contextlib.suppress(ProtocolError):
            decode_frames(bytes(mutated))


def test_ping_payload_roundtrip() -> None:
    payload = ping_payload({"v": 1, "persona": "balanced", "seed": 2**63 + 5})
    assert parse_ping(payload)["seed"] == 2**63 + 5
    with pytest.raises(ProtocolError):
        parse_ping(b"not json")
    with pytest.raises(ProtocolError):
        parse_ping(b"[1,2,3]")
