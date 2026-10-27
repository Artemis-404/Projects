"""Fixed-size cover records (FR-05, SDD 5.2).

A record is exactly ``record_size`` bytes of frames: DATA frames first, then one
PADDING frame that fills the rest.  Records are written to TLS in a single
``write`` call so each one becomes a single TLS record of constant length
(NFR-04).  ``record_size`` is capped at 16,384 B, the TLS 1.3 plaintext limit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from protocol.frames import (
    HEADER_SIZE,
    MAX_FRAME_PAYLOAD,
    STREAM_ID_SIZE,
    Frame,
    FrameType,
    ProtocolError,
    encode_frame,
    parse_stream_payload,
)

#: Frames whose payload counts as real user bytes (STREAM_DATA carries a stream id).
REAL_FRAME_TYPES = (FrameType.DATA, FrameType.STREAM_DATA)

MAX_RECORD_SIZE = 16_384
MIN_RECORD_SIZE = HEADER_SIZE


class CoverError(ProtocolError):
    """Raised when a record cannot be built or parsed without breaking FR-05."""


@dataclass(frozen=True, slots=True)
class PackedRecord:
    """A built record plus the number of payload bytes it consumed."""

    raw: bytes
    consumed: int
    frames: tuple[Frame, ...]

    @property
    def real_bytes(self) -> int:
        return sum(frame_real_bytes(f) for f in self.frames)

    @property
    def cover_bytes(self) -> int:
        return len(self.raw) - self.real_bytes


def validate_record_size(record_size: int) -> None:
    if not isinstance(record_size, int):
        raise CoverError("record_size must be an integer")
    if record_size < MIN_RECORD_SIZE or record_size > MAX_RECORD_SIZE:
        raise CoverError(f"record_size {record_size} outside [{MIN_RECORD_SIZE}, {MAX_RECORD_SIZE}]")


def build_record(
    payload: bytes,
    record_size: int,
    prefix: Sequence[Frame] = (),
) -> PackedRecord:
    """Build exactly ``record_size`` bytes carrying as much of ``payload`` as fits.

    ``prefix`` frames (PING or CLOSE used at session start) are written before the
    DATA frames.  Bytes of ``payload`` that do not fit are *not* dropped: the
    returned ``consumed`` tells the caller how much was taken, so the scheduler
    can keep the remainder queued for the next slot (FR-06).
    """
    validate_record_size(record_size)
    raw = bytearray()
    space = record_size

    for frame in prefix:
        encoded = encode_frame(frame)
        if len(encoded) > space:
            raise CoverError("prefix frame does not fit in the record")
        raw += encoded
        space -= len(encoded)

    data = memoryview(payload)
    offset = 0
    frames: list[Frame] = list(prefix)

    while offset < len(data) and space - HEADER_SIZE > 0:
        available = space - HEADER_SIZE
        chunk = min(len(data) - offset, available, MAX_FRAME_PAYLOAD)
        # Leave room for at least a 3-byte empty PADDING frame, never 1-2 bytes.
        leftover = space - chunk - HEADER_SIZE
        if 0 < leftover < HEADER_SIZE:
            chunk -= HEADER_SIZE - leftover
            leftover = space - chunk - HEADER_SIZE
        if chunk <= 0:
            break
        frame = Frame(FrameType.DATA, bytes(data[offset : offset + chunk]))
        raw += encode_frame(frame)
        frames.append(frame)
        offset += chunk
        space = leftover

    if space >= HEADER_SIZE:
        padding = Frame(FrameType.PADDING, bytes(space - HEADER_SIZE))
        raw += encode_frame(padding)
        frames.append(padding)
        space = 0

    if space != 0:
        raise CoverError(f"record packing left {space} byte(s) unaccounted for")
    if len(raw) != record_size:  # pragma: no cover - guarded by construction
        raise CoverError(f"record length {len(raw)} != {record_size}")

    return PackedRecord(bytes(raw), offset, tuple(frames))


def frame_real_bytes(frame: Frame) -> int:
    """Real (non-cover) payload bytes carried by one frame."""
    if frame.type is FrameType.DATA:
        return len(frame.payload)
    if frame.type is FrameType.STREAM_DATA:
        return max(0, len(frame.payload) - STREAM_ID_SIZE)
    return 0


def pack_frames(frames: Sequence[Frame], record_size: int) -> PackedRecord:
    """Pack already-formed frames into one record, padding the remainder.

    Used by the multiplexed channel (D21), where the sender decides which whole
    frames go into a record instead of handing over a byte payload.  Frames that
    do not fit raise :class:`CoverError`; the caller must choose a smaller set.
    """
    validate_record_size(record_size)
    raw = bytearray()
    real = 0
    for frame in frames:
        encoded = encode_frame(frame)
        if len(raw) + len(encoded) + HEADER_SIZE > record_size:
            raise CoverError("frames do not fit in the record")
        raw += encoded
        real += frame_real_bytes(frame)
    padding = record_size - len(raw)
    if padding < HEADER_SIZE:
        raise CoverError("frames leave no room for the padding frame")
    pad = Frame(FrameType.PADDING, bytes(padding - HEADER_SIZE))
    raw += encode_frame(pad)
    return PackedRecord(bytes(raw), real, (*frames, pad))


def padding_record(record_size: int) -> PackedRecord:
    """A record that carries no real bytes (cover only)."""
    return build_record(b"", record_size)


def parse_record(raw: bytes, record_size: int) -> list[Frame]:
    """Split a record into frames, rejecting anything that breaks FR-05."""
    if len(raw) != record_size:
        raise CoverError(f"record is {len(raw)} B, expected {record_size} B")
    frames: list[Frame] = []
    offset = 0
    while offset < record_size:
        remaining = record_size - offset
        if remaining < HEADER_SIZE:
            raise CoverError("record ends inside a frame header")
        type_code = raw[offset]
        length = int.from_bytes(raw[offset + 1 : offset + 3], "big")
        if length > MAX_FRAME_PAYLOAD:
            raise CoverError(f"frame length {length} B exceeds {MAX_FRAME_PAYLOAD} B")
        end = offset + HEADER_SIZE + length
        if end > record_size:
            raise CoverError("frame crosses the record boundary")
        frame = Frame(FrameType.parse(type_code), raw[offset + HEADER_SIZE : end])
        if frame.type is FrameType.STREAM_DATA:
            parse_stream_payload(frame.payload)  # rejects a missing stream id
        if frames and frame.type is not FrameType.PADDING and frames[-1].type is FrameType.PADDING:
            raise CoverError(f"{frame.type.name} frame after PADDING frame")
        frames.append(frame)
        offset = end
    return frames


def extract_data(frames: Sequence[Frame]) -> bytes:
    """Concatenate DATA payloads and drop PADDING (FR-05 receiver rule)."""
    return b"".join(f.payload for f in frames if f.type is FrameType.DATA)
