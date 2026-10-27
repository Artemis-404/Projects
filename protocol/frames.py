"""UQAB inner frame protocol (FR-04, SDD 5.1).

Wire format::

    +----------+--------------+----------------------+
    | Type 1 B | Length 2 B   | Payload (Length B)    |
    +----------+--------------+----------------------+

``Length`` is unsigned big-endian and counts payload bytes only.  Frames with a
payload longer than :data:`MAX_FRAME_PAYLOAD` (16,384 B, the TLS 1.3 plaintext
limit) or with an unknown type code are rejected; callers must close the
connection on :class:`ProtocolError` because the stream position is no longer
trustworthy (NFR-08 requires no crash, not resynchronisation).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

HEADER_SIZE: Final[int] = 3
MAX_FRAME_PAYLOAD: Final[int] = 16_384
MAX_FRAME_SIZE: Final[int] = MAX_FRAME_PAYLOAD + HEADER_SIZE


class ProtocolError(Exception):
    """Raised for any frame that violates the frozen FR-04 contract."""


class FrameType(IntEnum):
    """Frame type codes fixed by FR-04."""

    DATA = 0x01
    PADDING = 0x02
    PING = 0x03
    CLOSE = 0x04
    STREAM_OPEN = 0x05
    STREAM_DATA = 0x06
    STREAM_END = 0x07

    @classmethod
    def parse(cls, value: int) -> FrameType:
        try:
            return cls(value)
        except ValueError as exc:
            raise ProtocolError(f"unknown frame type {value:#04x}") from exc


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded frame."""

    type: FrameType
    payload: bytes = b""

    def __post_init__(self) -> None:
        length = len(self.payload)
        if length > MAX_FRAME_PAYLOAD:
            raise ProtocolError(f"frame payload {length} B exceeds {MAX_FRAME_PAYLOAD} B")
        if self.type not in FrameType:
            raise ProtocolError(f"unknown frame type {self.type!r}")


def encode_frame(frame: Frame) -> bytes:
    """Encode one frame into ``Type | Length | Payload``."""
    length = len(frame.payload)
    if length > MAX_FRAME_PAYLOAD:
        raise ProtocolError(f"frame payload {length} B exceeds {MAX_FRAME_PAYLOAD} B")
    if frame.type not in FrameType:
        raise ProtocolError(f"unknown frame type {frame.type!r}")
    return bytes((int(frame.type),)) + length.to_bytes(2, "big") + frame.payload


def encode_frames(frames: Iterable[Frame]) -> bytes:
    return b"".join(encode_frame(f) for f in frames)


def decode_frame(data: bytes, offset: int = 0) -> tuple[Frame, int]:
    """Decode the frame at ``offset``. Returns the frame and the next offset."""
    if offset < 0:
        raise ProtocolError("negative offset")
    remaining = len(data) - offset
    if remaining < HEADER_SIZE:
        raise ProtocolError("truncated frame header")
    type_code = data[offset]
    length = int.from_bytes(data[offset + 1 : offset + 3], "big")
    if length > MAX_FRAME_PAYLOAD:
        raise ProtocolError(f"frame length {length} B exceeds {MAX_FRAME_PAYLOAD} B")
    end = offset + HEADER_SIZE + length
    if end > len(data):
        raise ProtocolError("truncated frame payload")
    return Frame(FrameType.parse(type_code), data[offset + HEADER_SIZE : end]), end


def decode_frames(data: bytes) -> list[Frame]:
    """Decode a buffer that must contain whole frames only."""
    frames: list[Frame] = []
    offset = 0
    while offset < len(data):
        frame, offset = decode_frame(data, offset)
        frames.append(frame)
    return frames


class FrameParser:
    """Incremental parser for frames arriving on a stream.

    ``feed`` appends bytes and returns every complete frame; a malformed frame
    raises :class:`ProtocolError` immediately and the caller closes the
    connection (NFR-08).
    """

    def __init__(self, max_buffered: int = MAX_FRAME_SIZE * 4) -> None:
        self._buffer = bytearray()
        self._max = max_buffered
        self.closed = False

    def feed(self, data: bytes) -> list[Frame]:
        if self.closed:
            raise ProtocolError("parser is closed")
        if len(self._buffer) + len(data) > self._max:
            raise ProtocolError("frame buffer limit exceeded")
        self._buffer.extend(data)
        frames: list[Frame] = []
        while len(self._buffer) >= HEADER_SIZE:
            length = int.from_bytes(self._buffer[1:3], "big")
            if length > MAX_FRAME_PAYLOAD:
                raise ProtocolError(f"frame length {length} B exceeds {MAX_FRAME_PAYLOAD} B")
            total = HEADER_SIZE + length
            if len(self._buffer) < total:
                break
            frame, _ = decode_frame(bytes(self._buffer[:total]))
            del self._buffer[:total]
            frames.append(frame)
        if not self._buffer:
            self._buffer = bytearray()
        return frames

    def close(self) -> None:
        self.closed = True
        self._buffer = bytearray()


STREAM_ID_SIZE: Final[int] = 2
MAX_STREAM_DATA: Final[int] = MAX_FRAME_PAYLOAD - STREAM_ID_SIZE
MIN_STREAM_ID: Final[int] = 1
MAX_STREAM_ID: Final[int] = 65_535


def stream_payload(stream_id: int, body: bytes = b"") -> bytes:
    """``StreamId | Body`` for the multiplexed frame types (D21)."""
    if not MIN_STREAM_ID <= int(stream_id) <= MAX_STREAM_ID:
        raise ProtocolError(f"stream id {stream_id!r} outside [1, {MAX_STREAM_ID}]")
    return int(stream_id).to_bytes(STREAM_ID_SIZE, "big") + body


def parse_stream_payload(payload: bytes) -> tuple[int, bytes]:
    """Split a multiplexed frame payload into ``(stream id, body)``."""
    if len(payload) < STREAM_ID_SIZE:
        raise ProtocolError("multiplexed frame is shorter than a stream id")
    stream_id = int.from_bytes(payload[:STREAM_ID_SIZE], "big")
    if stream_id == 0:
        raise ProtocolError("stream id 0 is reserved")
    return stream_id, payload[STREAM_ID_SIZE:]


def ping_payload(obj: dict[str, object]) -> bytes:
    """Encode a PING payload as UTF-8 JSON (SDD 5.3)."""
    import json

    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


def parse_ping(payload: bytes) -> dict[str, object]:
    """Decode a PING JSON payload, rejecting anything that is not a JSON object."""
    import json

    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"invalid PING payload: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("PING payload must be a JSON object")
    return value
