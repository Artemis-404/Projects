"""Multiplexed, always-on shaped session (FR-06, FR-07, FR-13, D21).

A UQAB tunnel is *one* TLS connection carrying *one* record stream on the persona
timetable, and every Tor stream rides inside it as a multiplexed sub-stream.

Why this exists: with one tunnel per Tor stream (the pre-D21 design) the observer
sees nothing at all while the user is idle, and six parallel tunnels while a page
loads.  That leak is bigger than anything the persona hides, so the whole point of
the transport needs a session that outlives individual streams: the record stream
runs from the moment the engine starts until it stops, carrying STREAM_OPEN /
STREAM_DATA / STREAM_END frames when there is traffic and pure cover when there is
not.  The observer still sees exactly one shaped stream of fixed-size records.

Frame usage (protocol/frames.py):

* ``STREAM_OPEN``  ``StreamId | "host:port"`` — one Tor stream begins.
* ``STREAM_DATA``  ``StreamId | bytes`` — bulk data; fragmented over records.
* ``STREAM_END``   ``StreamId`` — one direction is finished.
* ``PING`` / ``CLOSE`` keep their session-level meaning.

The client allocates odd stream ids, the server even ones, so ids never collide
even though both sides can open streams.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from cover.scheduler import Counters, ShapedChannel, SlotRecorder, StreamQueue
from cover.timetable import Timetable
from persona.model import Persona
from protocol.frames import (
    Frame,
    FrameType,
    ProtocolError,
    parse_ping,
    parse_stream_payload,
    stream_payload,
)
from security.guard import SessionClosed, TunnelError

#: Read size on the upper (Tor/ORPort) side.  Small enough that one write always
#: leaves room for its STREAM_DATA header inside even the smallest record.
MAX_CHUNK = 4096

#: Per-stream ceiling for data that arrived from the tunnel but has not been
#: written to the upper socket yet.  A peer that pushes faster than the upper
#: socket drains would otherwise grow memory without bound.
MAX_STREAM_BUFFER = 1 << 20

#: Frame overheads: 3 B frame header, 2 B stream id.
FRAME_OVERHEAD = 5


class MuxError(TunnelError):
    """A multiplexing rule was violated; the session fails closed."""


@dataclass(eq=False)
class Stream:
    """One Tor stream carried inside the shared shaped session."""

    sid: int
    host: str
    port: int
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    inbox: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    #: Set once the upper socket exists (or once the dial has failed).
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    failed: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: tuple[asyncio.Task[Any], ...] = ()
    local_closed: bool = False
    remote_closed: bool = False
    inbox_bytes: int = 0
    error: str = ""
    bytes_up: int = 0
    bytes_down: int = 0
    started: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "host": self.host,
            "port": self.port,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
            "error": self.error,
        }


class MuxSession:
    """One shaped TLS tunnel shared by every Tor stream of a client or bridge."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        persona: Persona,
        seed: int,
        counters: Counters,
        floor_slot_ms: int = 500,
        queue_bytes: int = 262_144,
        recorder: SlotRecorder | None = None,
        on_period: Callable[[str], None] | None = None,
        on_ping: Callable[[dict[str, object]], None] | None = None,
        dial: Callable[[str, int], Awaitable[tuple[Any, Any]]] | None = None,
        max_streams: int = 128,
        log: logging.Logger | None = None,
    ) -> None:
        self.persona = persona
        self.seed = seed
        self.counters = counters
        self.floor_slot_ms = floor_slot_ms
        self.recorder = recorder
        self.on_period = on_period
        self.on_ping = on_ping
        self.dial = dial
        self.max_streams = max(1, int(max_streams))
        self.log = log or logging.getLogger("uqab.pt.mux")
        self.timetable = Timetable(persona, seed, floor_slot_ms)
        self.queue = StreamQueue(queue_bytes)
        self.streams: dict[int, Stream] = {}
        # Odd ids from the client, even ids from the bridge.
        self.next_sid = 1 if dial is None else 2
        self.closed = asyncio.Event()
        self.error: BaseException | None = None
        self.period = persona.start_state
        self._closed = False
        self._runner: asyncio.Task[Any] | None = None
        self._pending: set[asyncio.Task[Any]] = set()
        self.channel = ShapedChannel(
            reader,
            writer,
            self.timetable,
            counters,
            frame_queue=self.queue,
            on_frames=self._dispatch,
            on_period=self._set_period,
            recorder=recorder,
            floor_slot_ms=floor_slot_ms,
        )

    # ------------------------------------------------------------------ state
    @property
    def is_open(self) -> bool:
        return not self._closed and not self.closed.is_set()

    @property
    def stream_count(self) -> int:
        return len(self.streams)

    def _set_period(self, period: str) -> None:
        self.period = period
        if callable(self.on_period):
            self.on_period(period)

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Start the slot clock; cover records flow from this moment on."""
        self._runner = asyncio.create_task(self._run(), name="uqab-mux-session")

    async def _run(self) -> None:
        try:
            await self.channel.run()
            self.log.info("shaped session ended")
        except SessionClosed as exc:
            self.log.info("peer ended the session: %s", exc)
        except asyncio.CancelledError:
            raise
        except (TunnelError, OSError, ProtocolError) as exc:
            self.error = exc
            self.log.warning("tunnel failure: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive
            self.error = exc
            self.log.exception("unexpected tunnel failure")
        finally:
            self._closed = True
            self.queue.close()
            self._fail_streams("tunnel closed")
            self.closed.set()

    async def wait_closed(self) -> None:
        await self.closed.wait()

    async def close(self, reason: str = "session closing") -> None:
        """End the session and every stream inside it (fail closed)."""
        if self._closed:
            return
        self._closed = True
        self.queue.close()
        self._fail_streams(reason)
        if self._runner is not None and not self._runner.done():
            # Tell the peer instead of dropping the TLS connection mid-record.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.channel.send_close(), 1.0)
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._runner
        for task in list(self._pending):
            task.cancel()
        if self._pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.gather(*self._pending, return_exceptions=True)
        self.closed.set()

    # ----------------------------------------------------------------- streams
    def _allocate_sid(self) -> int:
        for _ in range(65_535):
            sid = self.next_sid
            self.next_sid += 2
            if self.next_sid > 65_535:
                self.next_sid = 1 if self.dial is None else 2
            if sid not in self.streams:
                return sid
        raise MuxError("stream id space exhausted")

    async def open(
        self,
        host: str,
        port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> Stream:
        """Open a new sub-stream and start pumping it (client side)."""
        if not self.is_open:
            raise MuxError("shaped session is not open")
        if len(self.streams) >= self.max_streams:
            raise MuxError(f"stream limit ({self.max_streams}) reached")
        sid = self._allocate_sid()
        stream = self._register(sid, host, port)
        self._attach(stream, reader, writer)
        target = f"{host}:{port}".encode()
        self.queue.put_control(Frame(FrameType.STREAM_OPEN, stream_payload(sid, target)))
        self.log.debug("stream %d open -> %s:%d", sid, host, port)
        return stream

    def _register(self, sid: int, host: str, port: int) -> Stream:
        """Reserve a stream slot immediately, before its upper socket exists.

        Registering first is what makes OPEN followed by DATA in the same record
        safe: the data lands in the stream's inbox and the downlink task writes it
        once the socket is attached.
        """
        loop = asyncio.get_running_loop()
        stream = Stream(sid=sid, host=host, port=port, started=loop.time())
        self.streams[sid] = stream
        stream.tasks = (asyncio.create_task(self._downlink(stream), name=f"uqab-down-{sid}"),)
        return stream

    def _attach(
        self,
        stream: Stream,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        stream.reader = reader
        stream.writer = writer
        stream.ready.set()
        stream.tasks = (
            *stream.tasks,
            asyncio.create_task(self._uplink(stream), name=f"uqab-up-{stream.sid}"),
        )

    def _fail_stream(self, stream: Stream, reason: str) -> None:
        """Close one stream without touching the session around it."""
        stream.error = stream.error or reason
        stream.failed = True
        stream.local_closed = True
        stream.remote_closed = True
        stream.ready.set()
        with contextlib.suppress(asyncio.QueueFull):
            stream.inbox.put_nowait(None)
        for task in stream.tasks:
            if not task.done():
                task.cancel()
        if self.streams.pop(stream.sid, None) is not None:
            self.log.info("stream %d closed: %s", stream.sid, stream.error)
        stream.done.set()

    async def _uplink(self, stream: Stream) -> None:
        """Upper socket -> STREAM_DATA frames."""
        reader = stream.reader
        assert reader is not None
        try:
            while True:
                data = await reader.read(MAX_CHUNK)
                if not data:
                    break
                stream.bytes_up += len(data)
                await self.queue.put(stream.sid, data)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            stream.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.log.debug("stream %d uplink finished (up %d B)", stream.sid, stream.bytes_up)
            if not self._closed:
                # Ordered after this stream's queued bytes, never ahead of them.
                await self.queue.put_end(stream.sid)
            self._finish(stream, local=True)

    async def _downlink(self, stream: Stream) -> None:
        """STREAM_DATA frames -> upper socket."""
        try:
            while True:
                data = await stream.inbox.get()
                if data is None:
                    break
                await stream.ready.wait()
                if stream.failed or stream.writer is None:
                    break
                stream.inbox_bytes = max(0, stream.inbox_bytes - len(data))
                stream.bytes_down += len(data)
                stream.writer.write(data)
                await stream.writer.drain()
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            stream.error = f"{type(exc).__name__}: {exc}"
        finally:
            writer = stream.writer
            with contextlib.suppress(Exception):
                if writer is not None and writer.can_write_eof():
                    writer.write_eof()
            self._finish(stream, remote=True)

    def _finish(self, stream: Stream, *, local: bool = False, remote: bool = False) -> None:
        stream.local_closed = stream.local_closed or local
        stream.remote_closed = stream.remote_closed or remote
        if not (stream.local_closed and stream.remote_closed):
            return
        if self.streams.pop(stream.sid, None) is not None:
            self.log.debug(
                "stream %d closed (up %d B, down %d B, %s)",
                stream.sid,
                stream.bytes_up,
                stream.bytes_down,
                stream.error or "clean",
            )
        for task in stream.tasks:
            if not task.done() and task is not asyncio.current_task():
                task.cancel()
        stream.done.set()

    def _fail_streams(self, reason: str) -> None:
        for stream in list(self.streams.values()):
            stream.error = stream.error or reason
            stream.local_closed = True
            stream.remote_closed = True
            with contextlib.suppress(asyncio.QueueFull):
                stream.inbox.put_nowait(None)
            for task in stream.tasks:
                if not task.done():
                    task.cancel()
            stream.done.set()
        self.streams.clear()

    # ---------------------------------------------------------------- dispatch
    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _dispatch(self, frames: list[Frame]) -> None:
        """Handle the frames of one received record."""
        for frame in frames:
            kind = frame.type
            if kind is FrameType.PADDING:
                continue
            if kind is FrameType.STREAM_DATA:
                sid, body = parse_stream_payload(frame.payload)
                stream = self.streams.get(sid)
                if stream is None or stream.remote_closed:
                    continue  # data for a stream that already ended: drop it
                stream.inbox_bytes += len(body)
                if stream.inbox_bytes > MAX_STREAM_BUFFER:
                    self.log.warning("stream %d over its receive buffer; closing it", sid)
                    self._fail_stream(stream, "receive buffer overflow")
                    continue
                stream.inbox.put_nowait(body)
            elif kind is FrameType.STREAM_END:
                sid, _ = parse_stream_payload(frame.payload)
                stream = self.streams.get(sid)
                if stream is not None and not stream.remote_closed:
                    stream.remote_closed = True
                    stream.inbox.put_nowait(None)
            elif kind is FrameType.STREAM_OPEN:
                self._begin_accept(frame)
            elif kind is FrameType.PING:
                if self.on_ping is not None:
                    try:
                        self.on_ping(parse_ping(frame.payload))
                    except ProtocolError as exc:
                        raise MuxError(f"malformed PING: {exc}") from exc
            elif kind is FrameType.CLOSE:
                raise SessionClosed("peer sent CLOSE")
            else:  # DATA has no meaning once streams are multiplexed
                raise MuxError(f"unexpected {kind.name} frame in a multiplexed session")

    def _begin_accept(self, frame: Frame) -> None:
        """Register a peer-opened stream *now*, dial it right after.

        Registration is synchronous on purpose: the client's first record usually
        carries STREAM_OPEN and the first STREAM_DATA together, and the data must
        find its stream already in the table.
        """
        sid, body = parse_stream_payload(frame.payload)
        if self.dial is None:
            raise MuxError("peer opened a stream but this side cannot dial")
        if sid in self.streams:
            raise MuxError(f"duplicate stream id {sid}")
        if len(self.streams) >= self.max_streams:
            self.log.warning("refusing stream %d: limit reached", sid)
            self.queue.put_control(Frame(FrameType.STREAM_END, stream_payload(sid)))
            return
        try:
            target = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MuxError("STREAM_OPEN target is not UTF-8") from exc
        host, _, raw_port = target.rpartition(":")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise MuxError(f"malformed STREAM_OPEN target {target!r}") from exc
        stream = self._register(sid, host or "?", port)
        self.log.info("stream %d for %s:%d", sid, host or "?", port)
        self._spawn(self._dial_stream(stream))

    async def _dial_stream(self, stream: Stream) -> None:
        """Dial the upper socket for a peer-opened stream, then attach it."""
        assert self.dial is not None
        try:
            reader, writer = await self.dial(stream.host, stream.port)
        except (OSError, TunnelError) as exc:
            self.log.warning("stream %d could not be dialled: %s", stream.sid, exc)
            self.queue.put_control(Frame(FrameType.STREAM_END, stream_payload(stream.sid)))
            self._fail_stream(stream, f"dial failed: {exc}")
            return
        if stream.sid not in self.streams:  # ended while we were dialling
            with contextlib.suppress(Exception):
                writer.close()
            return
        self._attach(stream, reader, writer)

    # ------------------------------------------------------------------ extras
    async def send_ping(self, payload: dict[str, object]) -> None:
        """Queue a session-level PING (used as a liveness probe by tests)."""
        from protocol.frames import ping_payload

        self.queue.put_control(Frame(FrameType.PING, ping_payload(payload)))

    def snapshot(self) -> dict[str, Any]:
        return {
            "persona": self.persona.id,
            "seed": self.seed,
            "period": self.period,
            "streams": len(self.streams),
            "pending_frames": self.queue.pending_bytes(),
            "open": self.is_open,
            "error": str(self.error) if self.error else "",
        }
