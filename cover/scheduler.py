"""Slot scheduler and shaped channel (FR-06, FR-08, FR-13, NFR-03, NFR-04, D4).

One record is written per persona slot, and real bytes only ever ride inside
those records.  Real data arrives in a bounded queue and waits for the next slot,
so user activity can never change how many records are sent, how large they are
or when they are sent.  The uplink and downlink share one timetable, which both
sides derive from the persona and the session seed.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cover.record import PackedRecord, build_record, extract_data, parse_record
from cover.timetable import Slot, Timetable
from protocol.frames import HEADER_SIZE, Frame, FrameType
from security.guard import SessionClosed, TunnelClosed, TunnelError

MAX_CHUNK = 65_536


@dataclass(slots=True)
class Counters:
    """Real and cover byte counters (SDD 6.2), read by the stats file."""

    real_up: int = 0
    real_down: int = 0
    cover_up: int = 0
    cover_down: int = 0

    def add_up(self, real: int, cover: int) -> None:
        self.real_up += real
        self.cover_up += cover

    def add_down(self, real: int, cover: int) -> None:
        self.real_down += real
        self.cover_down += cover

    @property
    def real_total(self) -> int:
        return self.real_up + self.real_down

    @property
    def cover_total(self) -> int:
        return self.cover_up + self.cover_down

    @property
    def total(self) -> int:
        return self.real_total + self.cover_total

    @property
    def overhead(self) -> float:
        """Total bytes divided by real bytes (NFR-02)."""
        real = self.real_total
        if real <= 0:
            return 0.0
        return round(self.total / real, 4)

    def snapshot(self) -> dict[str, int | float]:
        return {
            "real_up": self.real_up,
            "real_down": self.real_down,
            "cover_up": self.cover_up,
            "cover_down": self.cover_down,
            "overhead": self.overhead,
            "total": self.total,
        }

    def reset(self) -> None:
        self.real_up = self.real_down = self.cover_up = self.cover_down = 0


class ByteQueue:
    """Bounded FIFO of real bytes with backpressure, so memory stays bounded."""

    def __init__(self, max_bytes: int = 262_144) -> None:
        self._buffer = bytearray()
        self._max = max(1024, int(max_bytes))
        self._not_full = asyncio.Event()
        self._not_full.set()
        self._closed = False
        self.bytes_in = 0
        self.bytes_out = 0

    def __len__(self) -> int:
        return len(self._buffer)

    @property
    def closed(self) -> bool:
        return self._closed

    async def put(self, data: bytes) -> None:
        if self._closed:
            return
        if not data:
            return
        while len(self._buffer) >= self._max and not self._closed:
            self._not_full.clear()
            await self._not_full.wait()
        if self._closed:
            return
        self._buffer += data
        self.bytes_in += len(data)

    def take(self, count: int) -> bytes:
        if count <= 0 or not self._buffer:
            return b""
        chunk = bytes(self._buffer[:count])
        del self._buffer[:count]
        self.bytes_out += len(chunk)
        if len(self._buffer) < self._max:
            self._not_full.set()
        return chunk

    def push_front(self, data: bytes) -> None:
        """Return bytes that did not fit a record to the head of the queue."""
        if data:
            self._buffer[:0] = data

    def peek_pending(self) -> int:
        return len(self._buffer)

    async def wait_empty(self, limit_s: float = 5.0) -> bool:
        """Wait until every queued byte has been sent (graceful session end)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, limit_s)
        while self._buffer and loop.time() < deadline:
            self._not_full.set()
            await asyncio.sleep(0.01)
        return not self._buffer

    def clear(self) -> None:
        """Drop queued real bytes; used when the tunnel fails (FR-09)."""
        self._buffer.clear()
        self._not_full.set()

    def close(self) -> None:
        self._closed = True
        self._buffer.clear()
        self._not_full.set()


@dataclass(slots=True)
class SlotRecorder:
    """Collects slot timing error and writes one JSON line per slot (NFR-03)."""

    path: Path | None = None
    errors_ms: list[float] = field(default_factory=list)
    sent: int = 0
    _handle: Any = None

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")

    def record(self, slot: Slot, error_ms: float, size: int, actual: float) -> None:
        self.errors_ms.append(error_ms)
        self.sent += 1
        if self._handle is not None:
            self._handle.write(
                json.dumps(
                    {
                        "t": round(actual, 6),
                        "index": slot.index,
                        "state": slot.state,
                        "due": slot.due_s,
                        "size": size,
                        "burst": slot.burst,
                        "error_ms": round(error_ms, 4),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * q
        low = int(position)
        high = min(low + 1, len(ordered) - 1)
        weight = position - low
        return ordered[low] * (1 - weight) + ordered[high] * weight

    def summary(self) -> dict[str, float | int]:
        errors = self.errors_ms
        return {
            "n": len(errors),
            "p50_error_ms": round(self._percentile(errors, 0.50), 3),
            "p95_error_ms": round(self._percentile(errors, 0.95), 3),
            "max_error_ms": round(max(errors), 3) if errors else 0.0,
        }

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class ShapedChannel:
    """Sends one fixed-size record per slot and reads the peer's records.

    ``on_data`` receives real bytes recovered from the peer's records; ``on_ping``
    sees control PING frames; a CLOSE frame raises :class:`TunnelClosed` so the
    caller fails closed.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        timetable: Timetable,
        counters: Counters,
        out_queue: ByteQueue,
        *,
        on_data: Callable[[bytes], Awaitable[None]],
        on_ping: Callable[[dict[str, Any]], None] | None = None,
        on_period: Callable[[str], None] | None = None,
        recorder: SlotRecorder | None = None,
        floor_slot_ms: int = 500,
        skip_first: bool = True,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.timetable = timetable
        self.counters = counters
        self.queue = out_queue
        self.on_data = on_data
        self.on_ping = on_ping
        self.on_period = on_period
        self.recorder = recorder or SlotRecorder()
        self.floor_s = max(0.001, floor_slot_ms / 1000.0)
        self.skip_first = skip_first
        self.period = timetable.persona.start_state
        self.last_record_size = timetable.persona.state(
            timetable.persona.start_state
        ).record_size
        self._closing = False

    # ------------------------------------------------------------- uplink
    def _slots(self) -> Iterable[Slot]:
        if self.skip_first:
            for slot in self.timetable.slots():
                if slot.index == 0:
                    continue
                yield slot
        else:
            yield from self.timetable.slots()

    async def uplink(self) -> None:
        """The slot clock: exactly one record per slot, never more (FR-06)."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        last_send = t0
        for slot in self._slots():
            due = t0 + slot.due_s
            # Floor rate (FR-08): even a persona that asks for slower slots can
            # never push the interval past floor_slot_ms.
            due = min(due, last_send + self.floor_s) if slot.due_s > 0 else due
            delay = due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if self._closing:
                return
            # A record has room for at most one DATA frame of record_size - 3 B,
            # so take exactly that much and never lose a byte to padding.
            payload = self.queue.take(max(0, slot.record_size - HEADER_SIZE))
            packed = build_record(payload, slot.record_size)
            if packed.consumed < len(payload):  # pragma: no cover - defensive
                self.queue.push_front(payload[packed.consumed :])
            self.writer.write(packed.raw)
            await self.writer.drain()
            now = loop.time()
            self.last_record_size = slot.record_size
            last_send = now
            self.counters.add_up(packed.real_bytes, packed.cover_bytes)
            self.recorder.record(slot, (now - due) * 1000.0, len(packed.raw), now)
            if slot.state != self.period:
                self.period = slot.state
                if self.on_period is not None:
                    self.on_period(slot.state)

    # ----------------------------------------------------------- downlink
    async def downlink(self) -> None:
        """Read the peer's records on the same timetable and recover real bytes."""
        for slot in self._slots():
            try:
                raw = await self.reader.readexactly(slot.record_size)
            except asyncio.IncompleteReadError as exc:
                raise TunnelClosed(
                    f"peer closed mid-record ({len(exc.partial)} of {slot.record_size} B)"
                ) from exc
            frames = parse_record(raw, slot.record_size)
            data = extract_data(frames)
            for frame in frames:
                if frame.type is FrameType.CLOSE:
                    raise SessionClosed("peer ended the session")
                if frame.type is FrameType.PING and self.on_ping is not None:
                    from protocol.frames import parse_ping

                    self.on_ping(parse_ping(frame.payload))
            if data:
                self.counters.add_down(len(data), slot.record_size - len(data))
                await self.on_data(data)
            else:
                self.counters.add_down(0, slot.record_size)

    # ---------------------------------------------------------------- run
    async def run(self) -> None:
        uplink = asyncio.create_task(self.uplink(), name="uqab-uplink")
        downlink = asyncio.create_task(self.downlink(), name="uqab-downlink")
        try:
            done, pending = await asyncio.wait(
                {uplink, downlink}, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
            for task in pending:
                task.cancel()
        finally:
            for task in (uplink, downlink):
                if not task.done():
                    task.cancel()
            await asyncio.gather(uplink, downlink, return_exceptions=True)

    async def send_close(self) -> None:
        """End the session with a CLOSE frame inside a normal-sized record."""
        await self.send_control(Frame(FrameType.CLOSE, b""), self.last_record_size)

    async def send_control(self, frame: Frame, record_size: int) -> None:
        """Send a control frame in a normal-sized record (CLOSE at teardown)."""
        packed: PackedRecord = build_record(b"", record_size, prefix=(frame,))
        self.writer.write(packed.raw)
        await self.writer.drain()
        self.counters.add_up(0, len(packed.raw))

    async def stop(self) -> None:
        self._closing = True
        self.queue.close()


def now() -> float:
    return time.monotonic()


def ensure_positive(value: float, name: str) -> float:
    if value <= 0:
        raise TunnelError(f"{name} must be positive")
    return value
