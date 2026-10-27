"""End-to-end transport tests: SOCKS5 -> TLS -> shaped records -> ORPort.

Covers FR-01 to FR-09 and NFR-09/NFR-10 without needing Tor: the mock ORPort and
the SOCKS caller stand in for Tor's ORPort and Tor's client behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

import pytest

from config import Config
from persona.validator import Limits, load_persona
from pt.client import ClientPT
from pt.server import ServerPT
from pt.session import SessionParams, SessionRejected, client_handshake
from pt.stats import read_stats
from tests.conftest import run
from tests.support import SocksCaller, free_port, open_echo_server
from tunnel.tls import TLSSettings, open_tunnel


def quiet(name: str) -> logging.Logger:
    logger = logging.getLogger(f"uqab.test.{name}")
    logger.handlers = [logging.NullHandler()]
    logger.setLevel(logging.CRITICAL)
    logger.propagate = False
    return logger


async def wait_until(
    predicate: Callable[[], bool], limit_s: float = 8.0, what: str = "condition"
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class Stack:
    """A running bridge PT server plus client PT wired to a mock ORPort."""

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
        self.server_port = 0

    async def start(self, persona: str = "high") -> None:
        self.echo, self.orport = await open_echo_server()
        self.server = ServerPT(
            self.cfg,
            orport=("127.0.0.1", self.orport),
            bind=("127.0.0.1", 0),
            log=quiet("server"),
        )
        self.server_task = asyncio.create_task(self.server.serve(announce=False))
        await wait_until(lambda: self.server is not None and self.server.server is not None)
        listener = self.server.server
        assert listener is not None and listener.sockets
        self.server_port = int(listener.sockets[0].getsockname()[1])

        client_cfg = self.cfg.with_overrides(
            **{
                "bridge.host": "127.0.0.1",
                "bridge.pt_port": self.server_port,
                "tls.hostname": "uqab-bridge",
            }
        )
        self.client = ClientPT(client_cfg, persona_id=persona, log=quiet("client"))
        self.client_task = asyncio.create_task(self.client.serve(announce=False))
        await wait_until(lambda: self.client is not None and self.client.server is not None)
        client_listener = self.client.server
        assert client_listener is not None and client_listener.sockets
        self.client_port = int(client_listener.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        for task in (self.client_task, self.server_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        for server in (self.client, self.server):
            if server is not None:
                with contextlib.suppress(Exception):
                    await server.shutdown()
        if self.echo is not None:
            self.echo.close()
            with contextlib.suppress(Exception):
                await self.echo.wait_closed()


def test_socks_to_orport_roundtrip_is_shaped(
    cfg: Config, tls_settings: TLSSettings, limits: Limits
) -> None:
    async def scenario() -> None:
        stack = Stack(cfg, tls_settings)
        await stack.start(persona="high")
        try:
            caller = await SocksCaller.connect(stack.client_port, "persona=high")
            request = b"GET / HTTP/1.0\r\nHost: demo.uqab\r\n\r\n"
            await caller.send(request)
            echoed = await caller.recv(len(request), limit_s=15)
            assert echoed == request

            assert stack.client is not None
            active_client = stack.client
            await wait_until(
                lambda: active_client.guard.state.value == "connected", what="connected state"
            )

            assert stack.client is not None
            stats_path = stack.client.stats_path

            def stats_connected() -> bool:
                current = read_stats(stats_path)
                return bool(current and current.get("state") == "connected")

            await wait_until(stats_connected, what="stats file to report connected")

            def counters_moved() -> bool:
                current = read_stats(stats_path)
                return bool(
                    current
                    and current.get("real_up", 0) >= len(request)
                    and current.get("real_down", 0) >= len(request)
                )

            await wait_until(counters_moved, what="byte counters to reflect the round trip")
            stats = read_stats(stats_path)
            assert stats is not None
            assert stats["state"] == "connected"
            assert stats["persona"] == "high"
            assert stats["real_up"] >= len(request)
            assert stats["real_down"] >= len(request)
            assert stats["cover_up"] > 0
            assert stats["period"] in {"active", "light", "idle"}
            await caller.close()
        finally:
            await stack.stop()

    run(scenario(), timeout=90)


def test_persona_comes_from_socks_arguments(cfg: Config, tls_settings: TLSSettings) -> None:
    async def scenario() -> None:
        stack = Stack(cfg, tls_settings)
        await stack.start(persona="low")
        try:
            caller = await SocksCaller.connect(stack.client_port, "persona=constant")
            await caller.send(b"ping")
            assert await caller.recv(4) == b"ping"
            assert stack.client is not None
            stats = read_stats(stack.client.stats_path)
            assert stats is not None
            assert stats["persona"] == "constant"
            await caller.close()
        finally:
            await stack.stop()

    run(scenario(), timeout=90)


def test_socks_without_auth_method(cfg: Config, tls_settings: TLSSettings) -> None:
    async def scenario() -> None:
        stack = Stack(cfg, tls_settings)
        await stack.start(persona="balanced")
        try:
            caller = await SocksCaller.connect(
                stack.client_port, use_auth=False, host="127.0.0.1"
            )
            await caller.send(b"no-auth")
            assert await caller.recv(7) == b"no-auth"
            await caller.close()
        finally:
            await stack.stop()

    run(scenario(), timeout=90)


def test_bridge_rejects_unknown_persona(
    cfg: Config, tls_settings: TLSSettings
) -> None:
    async def scenario() -> None:
        echo, orport = await open_echo_server()
        server = ServerPT(cfg, orport=("127.0.0.1", orport), bind=("127.0.0.1", 0), log=quiet("s"))
        task = asyncio.create_task(server.serve(announce=False))
        await wait_until(lambda: server.server is not None)
        assert server.server is not None and server.server.sockets
        port = int(server.server.sockets[0].getsockname()[1])
        try:
            reader, writer = await open_tunnel("127.0.0.1", port, tls_settings)
            params = SessionParams(persona="ghost", sha256="0" * 64, seed=1)
            await client_handshake(reader, writer, params, cfg.get_int("cover.greeting_record_size"))
            raise AssertionError("handshake should have been rejected")
        except SessionRejected:
            pass
        except Exception as exc:  # the server may close before the client reads CLOSE
            assert "closed" in str(exc).lower() or "CLOSE" in str(exc)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await server.shutdown()
            echo.close()
            with contextlib.suppress(Exception):
                await echo.wait_closed()

    run(scenario(), timeout=60)


def test_bridge_rejects_persona_hash_mismatch(
    cfg: Config, tls_settings: TLSSettings, limits: Limits
) -> None:
    persona = load_persona("persona/profiles/high.json", limits)

    async def scenario() -> None:
        echo, orport = await open_echo_server()
        server = ServerPT(cfg, orport=("127.0.0.1", orport), bind=("127.0.0.1", 0), log=quiet("s2"))
        task = asyncio.create_task(server.serve(announce=False))
        await wait_until(lambda: server.server is not None)
        assert server.server is not None and server.server.sockets
        port = int(server.server.sockets[0].getsockname()[1])
        try:
            reader, writer = await open_tunnel("127.0.0.1", port, tls_settings)
            params = SessionParams(persona=persona.id, sha256="a" * 64, seed=7)
            with pytest.raises((SessionRejected, Exception)):
                await client_handshake(reader, writer, params, 1024)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await server.shutdown()
            echo.close()
            with contextlib.suppress(Exception):
                await echo.wait_closed()

    run(scenario(), timeout=60)


def test_server_handshake_accepts_matching_hash(
    cfg: Config, tls_settings: TLSSettings, limits: Limits
) -> None:
    persona = load_persona("persona/profiles/balanced.json", limits)
    personas = {persona.id: persona}

    async def scenario() -> None:
        echo, orport = await open_echo_server()
        server = ServerPT(cfg, orport=("127.0.0.1", orport), bind=("127.0.0.1", 0), log=quiet("s3"))
        task = asyncio.create_task(server.serve(announce=False))
        await wait_until(lambda: server.server is not None)
        assert server.server is not None and server.server.sockets
        port = int(server.server.sockets[0].getsockname()[1])
        try:
            reader, writer = await open_tunnel("127.0.0.1", port, tls_settings)
            params = SessionParams(persona=persona.id, sha256=persona.content_hash(), seed=42)
            await client_handshake(reader, writer, params, 1024)
            writer.close()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await server.shutdown()
            echo.close()
            with contextlib.suppress(Exception):
                await echo.wait_closed()
        assert personas[persona.id].content_hash() == persona.content_hash()

    run(scenario(), timeout=60)


def test_failure_closes_tor_stream_and_blocks(
    cfg: Config, tls_settings: TLSSettings
) -> None:
    async def scenario() -> None:
        stack = Stack(cfg, tls_settings)
        await stack.start(persona="high")
        caller = None
        try:
            caller = await SocksCaller.connect(stack.client_port, "persona=high")
            await caller.send(b"before")
            assert await caller.recv(6) == b"before"
            # Kill the bridge: the client must fail closed (FR-09, UC-6).
            assert stack.server is not None
            await stack.server.shutdown()
            if stack.server_task is not None:
                stack.server_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stack.server_task
            # The next record cannot be delivered; the Tor stream must close and
            # the state must become blocked, with no direct fallback.
            with contextlib.suppress(Exception):
                await caller.recv(1, limit_s=8)
            assert stack.client is not None
            failing_client = stack.client
            await wait_until(
                lambda: failing_client.guard.state.value in {"blocked", "stopped"},
                what="blocked state",
            )
            assert stack.client is not None
            blocked_path = stack.client.stats_path

            def stats_blocked() -> bool:
                current = read_stats(blocked_path)
                return bool(current and current.get("state") in {"blocked", "stopped"})

            await wait_until(stats_blocked, what="stats file to report the failure")
            stats = read_stats(blocked_path)
            assert stats is not None
            assert stats["state"] in {"blocked", "stopped"}
            assert stats["reason"], "a failure reason must be recorded for the panel"
        finally:
            if caller is not None:
                await caller.close()
            await stack.stop()

    run(scenario(), timeout=90)


def test_client_retries_and_recovers_within_retry_window(
    cfg: Config, tls_settings: TLSSettings
) -> None:
    async def scenario() -> None:
        echo, orport = await open_echo_server()
        port = free_port()
        server = ServerPT(
            cfg, orport=("127.0.0.1", orport), bind=("127.0.0.1", port), log=quiet("s4")
        )
        server_task = asyncio.create_task(server.serve(announce=False))
        await wait_until(lambda: server.server is not None)
        client_cfg = cfg.with_overrides(
            **{"bridge.pt_port": port, "cover.retry_s": 0.3, "pt.connect_timeout_s": 8}
        )
        client = ClientPT(client_cfg, persona_id="high", log=quiet("c4"))
        client_task = asyncio.create_task(client.serve(announce=False))
        await wait_until(lambda: client.server is not None)
        assert client.server is not None and client.server.sockets
        cport = int(client.server.sockets[0].getsockname()[1])
        try:
            caller = await SocksCaller.connect(cport, "persona=high")
            await caller.send(b"up")
            assert await caller.recv(2) == b"up"
            await caller.close()

            # Restart the bridge on the same port (NFR-10).
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
            await server.shutdown()
            await asyncio.sleep(0.2)
            loop = asyncio.get_running_loop()
            started = loop.time()
            server2 = ServerPT(
                cfg, orport=("127.0.0.1", orport), bind=("127.0.0.1", port), log=quiet("s5")
            )
            server_task = asyncio.create_task(server2.serve(announce=False))
            await wait_until(lambda: server2.server is not None)

            caller2 = await asyncio.wait_for(
                SocksCaller.connect(cport, "persona=high"), timeout=8
            )
            elapsed = loop.time() - started
            await caller2.send(b"down")
            assert await caller2.recv(4, limit_s=15) == b"down"
            assert elapsed <= 10.0, f"reconnect took {elapsed:.2f}s"
            await caller2.close()
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
            await server2.shutdown()
        finally:
            client_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await client_task
            with contextlib.suppress(Exception):
                await client.shutdown()
            echo.close()
            with contextlib.suppress(Exception):
                await echo.wait_closed()

    run(scenario(), timeout=120)


def test_startup_announce_lines_match_pt_spec(capsys: pytest.CaptureFixture[str]) -> None:
    from pt.client import ClientPT

    async def scenario() -> None:
        client = ClientPT(load_config_for_announce(), log=quiet("c6"))
        client.announce(12345)
        await client.stats.stop()

    run(scenario())
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "VERSION 1"
    assert out[1] == "CMETHOD uqab socks5 127.0.0.1:12345"
    assert out[2] == "CMETHODS DONE"


def load_config_for_announce() -> Config:
    from config import load_config

    return load_config()
