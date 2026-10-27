"""Unit tests for the PT client and server edges (FR-01, FR-02, FR-09, FR-12).

These cover the parts the end-to-end session tests cannot reach cheaply: the
managed-transport environment parsing, announcement formats, persona selection
fallbacks, the shutdown paths and the CLI argument handling.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import pytest

from config import Config
from lab.orport import MAGIC, encode_stream_header
from pt import socks5
from pt.client import ClientPT, PTEnv, validate_managed_env
from pt.server import METHOD, ServerEnv, ServerPT, split_hostport
from tests.conftest import run

# --------------------------------------------------------------------- envs


def test_client_env_defaults_and_flags() -> None:
    env = PTEnv.from_environ({})
    assert env.transports == "uqab"
    assert env.supports_v1() is True
    assert env.exit_on_stdin_close is False

    env = PTEnv.from_environ(
        {
            "TOR_PT_MANAGED_TRANSPORT_VER": "1, 2",
            "TOR_PT_CLIENT_TRANSPORTS": "uqab",
            "TOR_PT_STATE_LOCATION": "/tmp/state",
            "TOR_PT_EXIT_ON_STDIN_CLOSE": "1",
        }
    )
    assert env.supports_v1() is True
    assert env.state_location == "/tmp/state"
    assert env.exit_on_stdin_close is True


def test_validate_managed_env_rejects_unknown_versions() -> None:
    validate_managed_env(PTEnv(managed_versions="1"))
    with pytest.raises(SystemExit) as excinfo:
        validate_managed_env(PTEnv(managed_versions="2, 3"))
    assert excinfo.value.code == 2


def test_split_hostport_forms() -> None:
    assert split_hostport("127.0.0.1:9001") == ("127.0.0.1", 9001)
    assert split_hostport("[::1]:9443") == ("::1", 9443)
    assert split_hostport(" demo.uqab:80 ") == ("demo.uqab", 80)
    for bad in ("nohost", ":123", "host:"):
        with pytest.raises(ValueError):
            split_hostport(bad)


def test_server_env_parses_bind_and_orport() -> None:
    env = ServerEnv.from_environ(
        {
            "TOR_PT_SERVER_TRANSPORTS": "uqab",
            "TOR_PT_SERVER_BINDADDR": "uqab-0.0.0.0:9443,bogus-entry,other-not-an-addr",
            "TOR_PT_ORPORT": "127.0.0.1:9100",
            "TOR_PT_EXIT_ON_STDIN_CLOSE": "1",
        }
    )
    assert env.transports == ("uqab",)
    assert env.bind_addrs == {"uqab": ("0.0.0.0", 9443)}
    assert env.orport == ("127.0.0.1", 9100)
    assert env.exit_on_stdin_close is True


def test_server_env_falls_back_on_bad_values() -> None:
    env = ServerEnv.from_environ({"TOR_PT_SERVER_TRANSPORTS": " , ", "TOR_PT_ORPORT": "nope"})
    assert env.transports == (METHOD,)
    assert env.orport == ("127.0.0.1", 9001)


# ------------------------------------------------------------------- client


def test_persona_selection_falls_back(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    client = ClientPT(cfg)
    assert client.persona_for(None).id == client.persona_id
    assert client.persona_for("constant").id == "constant"
    # An unknown persona must not raise: Tor may send anything in the username.
    assert client.persona_for("does-not-exist").id == client.persona_id


def test_client_rejects_unknown_default_persona(cfg: Config) -> None:
    client = ClientPT(cfg, persona_id="nope")
    assert client.persona_id == "balanced"
    assert client.period


def test_client_snapshot_shape(cfg: Config) -> None:
    client = ClientPT(cfg)
    snapshot = client.snapshot()
    for key in (
        "t",
        "state",
        "persona",
        "period",
        "real_up",
        "real_down",
        "cover_up",
        "cover_down",
        "overhead",
        "bootstrap",
        "session_started",
        "reason",
        "sessions",
        "bridge",
    ):
        assert key in snapshot
    assert snapshot["state"] in {"stopped", "connecting", "connected", "blocked"}
    assert snapshot["bridge"].count(":") == 1


def test_client_announces_cmethod(capsys: pytest.CaptureFixture[str], cfg: Config) -> None:
    client = ClientPT(cfg)
    client.announce(12345)
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "VERSION 1"
    assert lines[1].startswith("CMETHOD uqab socks5 ")
    assert lines[1].endswith(":12345")
    assert lines[2] == "CMETHODS DONE"


def test_client_shutdown_is_idempotent(cfg: Config) -> None:
    client = ClientPT(cfg)

    async def body() -> None:
        await client.shutdown()
        await client.shutdown()

    run(body())
    assert client.server is None
    assert client.snapshot()["state"] in {"stopped", "blocked"}


def test_client_publish_writes_stats(cfg: Config) -> None:
    client = ClientPT(cfg)
    client.publish()
    stats_path = cfg.path("paths.run") / "stats.json"
    assert stats_path.is_file()


# ------------------------------------------------------------------- server


def test_server_announces_smethod(capsys: pytest.CaptureFixture[str], cfg: Config) -> None:
    server = ServerPT(cfg, orport=("127.0.0.1", 9001), bind=("127.0.0.1", 0))
    server.announce("127.0.0.1", 9443)
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "VERSION 1"
    assert lines[1].startswith("SMETHOD uqab ")
    assert lines[2] == "SMETHODS DONE"
    server.announce("::1", 9444)
    assert "[::1]:9444" in capsys.readouterr().out


def test_server_rejects_plaintext_connections(cfg: Config) -> None:
    """A client that opens TCP but sends no TLS must be dropped, not crashed."""

    async def body() -> tuple[int, int]:
        orport = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        assert orport.sockets
        or_port = orport.sockets[0].getsockname()[1]
        server = ServerPT(cfg, orport=("127.0.0.1", or_port), bind=("127.0.0.1", 0))
        task = asyncio.create_task(server.serve(announce=False))
        for _ in range(500):  # noqa: ASYNC110 - waiting for the listener, not a condition
            if server.server is not None:
                break
            await asyncio.sleep(0.01)
        listener = server.server
        assert listener is not None and listener.sockets
        port = listener.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"not a tls handshake")
        await writer.drain()
        data = b""
        with contextlib.suppress(Exception):
            data = await asyncio.wait_for(reader.read(64), timeout=5)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await server.shutdown()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        orport.close()
        with contextlib.suppress(Exception):
            await orport.wait_closed()
        return len(data), server.sessions

    received, sessions = run(body())
    assert sessions == 0
    assert received == 0


def test_server_survives_an_unreachable_orport(cfg: Config) -> None:
    """If the bridge's Tor is down the tunnel is refused, never half-open."""

    async def body() -> int:
        server = ServerPT(cfg, orport=("127.0.0.1", 1), bind=("127.0.0.1", 0))
        task = asyncio.create_task(server.serve(announce=False))
        for _ in range(500):  # noqa: ASYNC110 - waiting for the listener, not a condition
            if server.server is not None:
                break
            await asyncio.sleep(0.01)
        listener = server.server
        assert listener is not None and listener.sockets
        port = listener.sockets[0].getsockname()[1]
        # A TCP connect that closes immediately exercises the ORPort failure path
        # before any handshake can happen.
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await asyncio.sleep(0.2)
        await server.shutdown()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return server.sessions

    assert run(body()) == 0


# ---------------------------------------------------------------------- misc


def test_lab_stream_header_roundtrip() -> None:
    header = encode_stream_header("demo.uqab", 80)
    assert header.startswith(MAGIC)
    assert b"demo.uqab" in header
    assert header.endswith((80).to_bytes(2, "big"))


def test_socks5_error_codes_are_distinct() -> None:
    codes = {
        socks5.REP_SUCCESS,
        socks5.REP_GENERAL_FAILURE,
        socks5.REP_HOST_UNREACHABLE,
        socks5.REP_CMD_NOT_SUPPORTED,
    }
    assert len(codes) == 4


def test_modules_expose_main(cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`python -m pt.client --help` and the server equivalent must not crash."""
    from pt import client as client_module
    from pt import server as server_module

    for module in (client_module, server_module):
        with pytest.raises(SystemExit) as excinfo:
            module.main(["--help"])
        assert excinfo.value.code == 0
    assert callable(client_module.main)
    assert callable(server_module.main)
    assert sys.executable
