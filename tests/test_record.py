"""Cover record tests (FR-05, NFR-04)."""

from __future__ import annotations

import random

import pytest

from cover.record import (
    MAX_RECORD_SIZE,
    CoverError,
    build_record,
    extract_data,
    padding_record,
    parse_record,
    validate_record_size,
)
from protocol.frames import HEADER_SIZE, Frame, FrameType


@pytest.mark.parametrize("record_size", [3, 4, 5, 8, 64, 512, 1024, 4096, MAX_RECORD_SIZE])
@pytest.mark.parametrize("payload_len", [0, 1, 2, 3, 7, 100, 1024, 5000])
def test_record_is_exactly_record_size(record_size: int, payload_len: int) -> None:
    payload = bytes((i * 11) % 256 for i in range(payload_len))
    packed = build_record(payload, record_size)
    assert len(packed.raw) == record_size
    assert packed.consumed <= min(payload_len, record_size)
    frames = parse_record(packed.raw, record_size)
    # Data received is a prefix of what was offered, and nothing is corrupted.
    assert extract_data(frames) == payload[: packed.consumed]


def test_data_frames_come_first_then_one_padding_frame() -> None:
    packed = build_record(b"x" * 100, 1024)
    frames = parse_record(packed.raw, 1024)
    types = [f.type for f in frames]
    assert types[-1] is FrameType.PADDING
    assert all(t is FrameType.DATA for t in types[:-1])
    assert sum(len(f.payload) for f in frames) + HEADER_SIZE * len(frames) == 1024


def test_padding_never_leaves_one_or_two_stray_bytes() -> None:
    for record_size in range(3, 80):
        for payload_len in range(0, 40):
            packed = build_record(b"z" * payload_len, record_size)
            assert len(packed.raw) == record_size
            parse_record(packed.raw, record_size)
            for frame in packed.frames:
                assert len(frame.payload) + HEADER_SIZE <= record_size


def test_large_payload_is_split_across_records_without_loss() -> None:
    payload = bytes(range(256)) * 20
    rest = payload
    out = b""
    while rest:
        packed = build_record(rest, 512)
        out += b"".join(f.payload for f in packed.frames if f.type is FrameType.DATA)
        rest = rest[packed.consumed :]
    assert out == payload


def test_full_record_without_padding_space() -> None:
    # record_size 4 == header + 1 payload byte: a single DATA frame fills it.
    packed = build_record(b"AB", 4)
    assert packed.raw == bytes([0x01, 0x00, 0x01]) + b"A"
    assert packed.consumed == 1


def test_prefix_frames_and_padding_fill() -> None:
    ping = Frame(FrameType.PING, b"{}")
    packed = build_record(b"", 1024, prefix=(ping,))
    frames = parse_record(packed.raw, 1024)
    assert frames[0].type is FrameType.PING
    assert frames[-1].type is FrameType.PADDING
    assert len(packed.raw) == 1024
    assert packed.consumed == 0


def test_prefix_that_does_not_fit_is_rejected() -> None:
    with pytest.raises(CoverError):
        build_record(b"", 4, prefix=(Frame(FrameType.PING, b"12345"),))


def test_padding_record_has_no_real_bytes() -> None:
    packed = padding_record(512)
    assert packed.consumed == 0
    assert packed.real_bytes == 0
    assert packed.cover_bytes == 512


@pytest.mark.parametrize("record_size", [0, 2, -1, MAX_RECORD_SIZE + 1])
def test_invalid_record_size_rejected(record_size: int) -> None:
    with pytest.raises(CoverError):
        build_record(b"", record_size)
    with pytest.raises(CoverError):
        validate_record_size(record_size)


def test_parse_record_rejects_wrong_length_and_truncation() -> None:
    with pytest.raises(CoverError):
        parse_record(b"\x02\x00\x00", 4)
    with pytest.raises(CoverError):
        # Header claims 10 payload bytes inside an 8-byte record.
        parse_record(bytes([0x01, 0x00, 0x0A]) + b"\x00" * 5, 8)


def test_parse_record_rejects_data_after_padding() -> None:
    raw = bytes([0x02, 0x00, 0x00]) + bytes([0x01, 0x00, 0x00])
    with pytest.raises(CoverError):
        parse_record(raw, 6)


def test_random_payloads_never_produce_bad_records() -> None:
    rng = random.Random(5)
    for _ in range(300):
        record_size = rng.randrange(3, 300)
        payload = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 400)))
        packed = build_record(payload, record_size)
        assert len(packed.raw) == record_size
        frames = parse_record(packed.raw, record_size)
        assert extract_data(frames) == payload[: packed.consumed]
