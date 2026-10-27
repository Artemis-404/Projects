"""Multiplexed, always-on shaped session (D21).

These tests pin the three properties the research question depends on:

1. every Tor stream of a client shares **one** shaped tunnel (no burst of tunnels
   when a page opens six connections),
2. the record stream keeps running — as cover — after the last stream closes, so
   an observer never sees an "idle means silence" gap,
3. a tunnel failure fails every stream closed instead of leaking.

Plus unit tests for the frame-level queue that makes 1 and 2 possible.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from config import Config
from cover.record import CoverError, pack_frames, parse_record
from cover.scheduler import StreamQueue
from protocol.frames import (
    Frame,
    FrameType,
    ProtocolError,
    parse_stream_payload,
    stream_payload,
)
from pt.client import ClientPT
from pt.server import ServerPT
from pt.stats import read_stats
from security.guard import close_server
from tests.conftest import run
from tests.support import SocksCaller, open_echo_server
from tunnel.tls import TLSSettings


def quiet(name: str) -> logging.Logger:
    logger = logging.getLogger(f"uqab.test.mux.{name}")
    logger.handlers = [logging.NullHandler()]
    logger.setLevel(logging.CRITICAL)
    logger.propagate = False
    return logger


# --------------------------------------------------------------------- units
def test_stream_payload_roundtrip() -> None:
    payload = stream_payload(7, b"hello")
    assert parse_stream_payload(payload) == (7, b"hello")


@pytest.mark.parametrize("sid", [0, 65_536, -1])
def test_stream_ids_are_validated(sid: int) -> None:
    with pytest.raises(ProtocolError):
        stream_payload(sid, b"")


def test_stream_payload_requires_an_id() -> None:
    with pytest.raises(ProtocolError):
        parse_stream_payload(b"\x01")


def test_pack_frames_fills_a_record_and_counts_real_bytes() -> None:
    frames = [
        Frame(FrameType.STREAM_DATA, stream_payload(1, b"a" * 100)),
        Frame(FrameType.STREAM_END, stream_payload(1)),
    ]
    packed = pack_frames(frames, 2048)
    assert len(packed.raw) == 2048
    assert packed.real_bytes == 100
    decoded = parse_record(packed.raw, 2048)
    assert [frame.type for frame in decoded[:2]] == [FrameType.STREAM_DATA, FrameType.STREAM_END]
    assert decoded[-1].type is FrameType.PADDING


def test_pack_frames_rejects_frames_that_do_not_fit() -> None:
    frames = [Frame(FrameType.STREAM_DATA, stream_payload(1, b"x" * 4000))]
    with pytest.raises(CoverError):
        pack_frames(frames, 1024)


def test_stream_queue_fragments_a_large_write() -> None:
    async def scenario() -> list[int]:
        queue = StreamQueue(65_536)
        await queue.put(3, b"y" * 1000)
        sizes: list[int] = []
        for _ in range(10):
            frames, real = queue.take(512)
            sizes.append(real)
            for frame in frames:
                sid, body = parse_stream_payload(frame.payload)
                assert sid == 3
            if real == 0:
                break
        return sizes

    sizes = run(scenario())
    assert sum(sizes) == 1000
    assert all(size <= 512 for size in sizes)


def test_stream_queue_keeps_end_behind_data() -> None:
    async def scenario() -> list[str]:
        queue = StreamQueue(65_536)
        await queue.put(9, b"z" * 2000)
        await queue.put_end(9)
        order: list[str] = []
        while True:
            frames, real = queue.take(256)
            if not frames and not real:
                break
            order.extend(frame.type.name for frame in frames)
            if FrameType.STREAM_END in [frame.type for frame in frames]:
                break
        return order

    order = run(scenario())
    assert order[-1] == "STREAM_END"
    assert order.count("STREAM_DATA") > 1


def test_stream_queue_open_is_prioritised() -> None:
    async def scenario() -> list[FrameType]:
        queue = StreamQueue(65_536)
        await queue.put(1, b"data" * 100)
        queue.put_control(Frame(FrameType.STREAM_OPEN, stream_payload(2, b"h:1")))
        frames, _ = queue.take(512)
        return [frame.type for frame in frames]

    assert run(scenario())[0] is FrameType.STREAM_OPEN


# ------------------------------------------------------------------ end to end
class Tunnel:
    """A bridge PT and a client PT wired to a mock ORPort, like Tor would."""

    def __init__(self, cfg: Config, tls: TLSSettings) -> None:
        self.cfg = cfg
        self.tls = tls
        self.echo: asyncio.Server | None = None
        self.orport = 0
        self.server: ServerPT | None = None
        self.server_task: asyncio.Task[None] | None = None
        self.client: ClientPT | None = None
        self.client_task: asyncio.Task[None] | None = None
        self.client_port = 0

    async def start(self, persona: str = "high") -> None:
        self.echo, self.orport = await open_echo_server()
        self.server = ServerPT(
            self.cfg,
            orport=("127.0.0.1", self.orport),
            bind=("127.0.0.1", 0),
            log=quiet("server"),
        )
        self.server_task = asyncio.create_task(self.server.serve(announce=False))
        for _ in range(500):  # noqa: ASYNC110 - polls an attribute, not an event
            if self.server.server is not None:
                break
            await asyncio.sleep(0.01)
        listener = self.server.server
        assert listener is not None
        port = int(listener.sockets[0].getsockname()[1])
        client_cfg = self.cfg.with_overrides(
            **{
                "bridge.host": "127.0.0.1",
                "bridge.pt_port": port,
                "tls.hostname": "uqab-bridge",
            }
        )
        self.client = ClientPT(client_cfg, persona_id=persona, log=quiet("client"))
        self.client_task = asyncio.create_task(self.client.serve(announce=False))
        for _ in range(500):  # noqa: ASYNC110 - poll an attribute, not an event
            if self.client.server is not None:
                break
            await asyncio.sleep(0.01)
        client_listener = self.client.server
        assert client_listener is not None
        self.client_port = int(client_listener.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        for task in (self.client_task, self.server_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.shutdown()
        if self.server is not None:
            with contextlib.suppress(Exception):
                await self.server.shutdown()
        if self.echo is not None:
            await close_server(self.echo)


def test_parallel_streams_share_one_tunnel(cfg: Config, tls_settings: TLSSettings) -> None:
    """Six concurrent streams must produce exactly one shaped tunnel (D21)."""

    async def scenario() -> None:
        tunnel = Tunnel(cfg, tls_settings)
        await tunnel.start()
        try:
            callers = [await SocksCaller.connect(tunnel.client_port, "persona=high") for _ in range(6)]
            try:
                for index, caller in enumerate(callers):
                    payload = f"stream-{index}".encode()
                    await caller.send(payload)
                    assert await caller.recv(len(payload), limit_s=20) == payload
                assert tunnel.client is not None and tunnel.server is not None
                assert tunnel.server.sessions == 1
                session = tunnel.client.session
                assert session is not None
                assert session.stream_count == 6
            finally:
                for caller in callers:
                    await caller.close()
            # Streams drain asynchronously once the callers hang up.
            deadline = asyncio.get_running_loop().time() + 10
            session = tunnel.client.session
            assert session is not None
            while session.stream_count and asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
            assert session.stream_count == 0
        finally:
            await tunnel.stop()

    run(scenario(), timeout=120)


def test_cover_keeps_running_after_the_last_stream(cfg: Config, tls_settings: TLSSettings) -> None:
    """IDLE must look like cover, not like silence: the slot clock never stops."""

    async def scenario() -> None:
        tunnel = Tunnel(cfg, tls_settings)
        await tunnel.start()
        try:
            caller = await SocksCaller.connect(tunnel.client_port, "persona=high")
            await caller.send(b"ping")
            assert await caller.recv(4, limit_s=20) == b"ping"
            await caller.close()
            assert tunnel.client is not None
            session = tunnel.client.session
            assert session is not None
            deadline = asyncio.get_running_loop().time() + 10
            while session.stream_count and asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
            assert session.stream_count == 0

            stats = read_stats(tunnel.client.stats_path)
            assert stats is not None
            cover_before = int(stats["cover_up"])
            # Nothing is happening: only cover records should be sent, and they
            # must keep being sent.
            await asyncio.sleep(2.0)
            stats = read_stats(tunnel.client.stats_path)
            assert stats is not None
            assert stats["state"] == "connected"
            assert int(stats["cover_up"]) > cover_before
            assert session.is_open
            assert tunnel.server is not None and tunnel.server.sessions == 1
        finally:
            await tunnel.stop()

    run(scenario(), timeout=120)


def test_stream_is_closed_when_the_tunnel_dies(cfg: Config, tls_settings: TLSSettings) -> None:
    """Fail closed: a dead tunnel closes every Tor stream inside it."""

    async def scenario() -> None:
        tunnel = Tunnel(cfg, tls_settings)
        await tunnel.start()
        try:
            caller = await SocksCaller.connect(tunnel.client_port, "persona=high")
            await caller.send(b"hello")
            assert await caller.recv(5, limit_s=20) == b"hello"
            assert tunnel.server is not None and tunnel.client is not None
            session = tunnel.client.session
            assert session is not None
            await tunnel.server.shutdown()
            await asyncio.wait_for(session.closed.wait(), timeout=20)
            assert not session.is_open
            with pytest.raises((ConnectionError, OSError, asyncio.IncompleteReadError)):
                await caller.recv(1, limit_s=5)
            stats = read_stats(tunnel.client.stats_path)
            assert stats is not None
            assert stats["state"] in {"blocked", "connecting", "stopped"}
            assert stats["reason"] or stats["state"] == "stopped"
        finally:
            await tunnel.stop()

    run(scenario(), timeout=120)


def test_tunnel_is_up_before_the_first_page(cfg: Config, tls_settings: TLSSettings) -> None:
    """The cover tunnel starts with the client, not with the first request (D21).

    The app's tab gate only opens on `connected`; if the tunnel waited for a page
    nothing could ever load, and cover would not exist during idle.
    """

    async def scenario() -> None:
        tunnel = Tunnel(cfg, tls_settings)
        await tunnel.start()
        try:
            assert tunnel.client is not None
            deadline = asyncio.get_running_loop().time() + 20
            while tunnel.client.session is None and asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
            assert tunnel.client.session is not None
            assert tunnel.client.session.stream_count == 0
            stats = read_stats(tunnel.client.stats_path)
            assert stats is not None
            assert stats["state"] == "connected"
            # Cover starts immediately: wait for the first records to be counted.
            deadline = asyncio.get_running_loop().time() + 15
            while asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110
                stats = read_stats(tunnel.client.stats_path)
                if stats and int(stats["cover_up"]) > 0:
                    break
                await asyncio.sleep(0.05)
            assert stats is not None and int(stats["cover_up"]) > 0
            assert tunnel.server is not None and tunnel.server.sessions == 1
        finally:
            await tunnel.stop()

    run(scenario(), timeout=120)
