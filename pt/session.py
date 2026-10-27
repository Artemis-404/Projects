"""Session start and shaped byte streams (FR-07, SDD 5.3, 7.1).

The client's first record carries a PING frame with the session parameters
(``{"v":1,"persona":...,"sha256":...,"seed":...}``) and padding.  The server
checks that it holds the persona and that its hash matches, then answers with a
PING ``{"v":1,"ok":true}``.  Both sides then build the same timetable from the
persona and the seed and start their slot loops.

The greeting record has a fixed, persona-independent size
(``cover.greeting_record_size``), so an observer cannot learn the persona from
the first record and the server does not need the timetable before it knows the
persona.  This is recorded as design clarification D5a in
``docs/DESIGN_DECISIONS.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cover.scheduler import ByteQueue, Counters, ShapedChannel, SlotRecorder
from cover.timetable import Timetable
from persona.model import Persona
from protocol.frames import Frame, FrameType, ProtocolError, parse_ping, ping_payload
from security.guard import TunnelClosed, TunnelError

GREETING_VERSION = 1
MAX_CHUNK = 65_536


class SessionRejected(TunnelError):
    """Session parameters were unacceptable; the server sends CLOSE (SDD 5.3)."""


@dataclass(frozen=True, slots=True)
class SessionParams:
    """Session parameters carried inside the first record (SDD 6.2)."""

    persona: str
    sha256: str
    seed: int
    v: int = GREETING_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"v": self.v, "persona": self.persona, "sha256": self.sha256, "seed": self.seed}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionParams:
        version = data.get("v")
        if version != GREETING_VERSION:
            raise SessionRejected(f"unsupported session version {version!r}")
        persona = data.get("persona")
        if not isinstance(persona, str) or not persona:
            raise SessionRejected("session persona missing")
        digest = data.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise SessionRejected("session persona hash malformed")
        seed = data.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**64:
            raise SessionRejected("session seed malformed")
        return cls(persona=persona, sha256=digest, seed=seed, v=version)


async def send_record(
    writer: asyncio.StreamWriter,
    record_size: int,
    frames: tuple[Frame, ...] = (),
) -> int:
    """Write one record in a single TLS write (NFR-04)."""
    from cover.record import build_record

    packed = build_record(b"", record_size, prefix=frames)
    writer.write(packed.raw)
    await writer.drain()
    return len(packed.raw)


async def read_record(reader: asyncio.StreamReader, record_size: int) -> list[Frame]:
    from cover.record import parse_record

    try:
        raw = await reader.readexactly(record_size)
    except asyncio.IncompleteReadError as exc:
        raise TunnelClosed(
            f"connection closed while reading a {record_size} B record "
            f"({len(exc.partial)} B received)"
        ) from exc
    return parse_record(raw, record_size)


def _control_ping(frames: list[Frame]) -> dict[str, Any] | None:
    for frame in frames:
        if frame.type is FrameType.CLOSE:
            raise TunnelClosed("peer sent CLOSE during session start")
        if frame.type is FrameType.PING:
            try:
                return parse_ping(frame.payload)
            except ProtocolError as exc:
                raise SessionRejected(f"malformed PING: {exc}") from exc
    return None


async def client_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    params: SessionParams,
    greeting_size: int,
) -> None:
    """Send the session parameters and require a positive server answer."""
    await send_record(
        writer,
        greeting_size,
        (Frame(FrameType.PING, ping_payload(params.to_dict())),),
    )
    frames = await read_record(reader, greeting_size)
    answer = _control_ping(frames)
    if answer is None:
        raise SessionRejected("server did not answer the session PING")
    if answer.get("ok") is not True:
        raise SessionRejected(f"server rejected the session: {answer!r}")


async def server_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    personas: Mapping[str, Persona],
    greeting_size: int,
    log: logging.Logger | None = None,
) -> tuple[SessionParams, Persona]:
    """Read the client's parameters, validate them and acknowledge."""
    logger = log or logging.getLogger("uqab.pt.server")
    frames = await read_record(reader, greeting_size)
    payload = _control_ping(frames)
    if payload is None:
        await send_record(writer, greeting_size, (Frame(FrameType.CLOSE, b"no-params"),))
        raise SessionRejected("first record carried no session parameters")
    params = SessionParams.from_dict(payload)
    persona = personas.get(params.persona)
    if persona is None:
        await send_record(writer, greeting_size, (Frame(FrameType.CLOSE, b"unknown-persona"),))
        raise SessionRejected(f"unknown persona {params.persona!r}")
    expected = persona.content_hash()
    if expected != params.sha256:
        await send_record(writer, greeting_size, (Frame(FrameType.CLOSE, b"persona-mismatch"),))
        raise SessionRejected(
            f"persona hash mismatch for {params.persona!r} "
            f"(server {expected[:12]}.., client {params.sha256[:12]}..)"
        )
    await send_record(writer, greeting_size, (Frame(FrameType.PING, ping_payload({"v": 1, "ok": True})),))
    logger.info(
        "session accepted persona=%s seed=%d", params.persona, params.seed
    )
    return params, persona


class PTStream:
    """One shaped session over a Tor (client) or ORPort (server) byte stream."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        persona: Persona,
        seed: int,
        counters: Counters,
        upper_reader: asyncio.StreamReader,
        upper_writer: asyncio.StreamWriter,
        floor_slot_ms: int = 500,
        queue_bytes: int = 262_144,
        recorder: SlotRecorder | None = None,
        on_period: Any = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.persona = persona
        self.seed = seed
        self.counters = counters
        self.upper_reader = upper_reader
        self.upper_writer = upper_writer
        self.floor_slot_ms = floor_slot_ms
        self.queue = ByteQueue(queue_bytes)
        self.recorder = recorder
        self.on_period = on_period
        self.log = log or logging.getLogger("uqab.pt.session")
        self.period = persona.start_state
        self._channel: ShapedChannel | None = None

    # ------------------------------------------------------------- internal
    async def _forward_to_upper(self, data: bytes) -> None:
        self.upper_writer.write(data)
        await self.upper_writer.drain()

    async def _pump_upper(self) -> None:
        while True:
            data = await self.upper_reader.read(MAX_CHUNK)
            if not data:
                return
            await self.queue.put(data)

    def _set_period(self, period: str) -> None:
        self.period = period
        if callable(self.on_period):
            self.on_period(period)

    # ------------------------------------------------------------------ api
    @property
    def channel(self) -> ShapedChannel:
        if self._channel is None:
            self._channel = ShapedChannel(
                self.reader,
                self.writer,
                Timetable(self.persona, self.seed, self.floor_slot_ms),
                self.counters,
                self.queue,
                on_data=self._forward_to_upper,
                on_period=self._set_period,
                recorder=self.recorder,
                floor_slot_ms=self.floor_slot_ms,
            )
        return self._channel

    async def run(self) -> None:
        """Pump real bytes into the scheduler and run the shaped channel."""
        channel = self.channel
        pump = asyncio.create_task(self._pump_upper(), name="uqab-upper-pump")
        shaped = asyncio.create_task(channel.run(), name="uqab-channel")
        try:
            done, pending = await asyncio.wait(
                {pump, shaped}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc
            # The upper stream closed first: flush whatever is still queued so a
            # response is never truncated, then end the session with CLOSE.
            if pump.done() and not shaped.done():
                drained = await self.queue.wait_empty(5.0)
                if not drained:
                    self.log.warning("queue not fully drained at session end")
                with contextlib.suppress(Exception):
                    await channel.send_close()
                self.log.debug("upper stream closed; session ended cleanly")
        finally:
            for task in (pump, shaped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(pump, shaped, return_exceptions=True)
            self.queue.close()

    async def close(self) -> None:
        self.queue.close()
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), timeout=2.0)
        except Exception:
            pass
