"""Mutual TLS 1.3 tests (FR-03, NFR-07)."""

from __future__ import annotations

import shutil
import ssl
import subprocess
from pathlib import Path

import pytest

from config import load_config
from tests.conftest import run
from tests.support import free_port
from tunnel.certs import CertPaths, check, generate
from tunnel.tls import (
    TLSSettings,
    TunnelError,
    assert_tls13,
    client_context,
    open_tunnel,
    peer_subject,
    server_context,
)


def test_mutual_tls13_handshake_and_data(tls_settings: TLSSettings) -> None:
    import asyncio

    seen: dict[str, str] = {}
    port = free_port()

    async def scenario() -> bytes:
        ctx = server_context(tls_settings)

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            seen["subject"] = peer_subject(writer)
            seen["version"] = str(writer.get_extra_info("ssl_object").version())
            data = await reader.readexactly(4)
            writer.write(b"pong" + data)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", port, ssl=ctx)
        try:
            reader, writer = await open_tunnel("127.0.0.1", port, tls_settings)
            writer.write(b"ping")
            await writer.drain()
            reply = await reader.readexactly(8)
            writer.close()
            return reply
        finally:
            server.close()
            await server.wait_closed()

    reply = run(scenario())
    assert reply == b"pongping"
    assert seen["version"] == "TLSv1.3"
    assert "uqab-client" in seen["subject"]


def test_client_without_certificate_is_rejected(tls_settings: TLSSettings) -> None:
    import asyncio

    port = free_port()

    async def scenario() -> None:
        ctx = server_context(tls_settings)

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", port, ssl=ctx)
        bare = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        bare.minimum_version = ssl.TLSVersion.TLSv1_3
        bare.load_verify_locations(cafile=tls_settings.ca)
        try:
            # TLS 1.3 can report the server's refusal after the client handshake
            # returns, so the rejection is observed either at connect or on use.
            with pytest.raises((ssl.SSLError, OSError, ConnectionError, asyncio.IncompleteReadError)):
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        "127.0.0.1",
                        port,
                        ssl=bare,
                        server_hostname=tls_settings.hostname,
                    ),
                    timeout=10,
                )
                writer.write(b"hello")
                await writer.drain()
                await asyncio.wait_for(reader.readexactly(1), timeout=5)
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())


def test_client_certificate_from_unknown_ca_is_rejected(
    tls_settings: TLSSettings, cert_dir: Path, tmp_path: Path
) -> None:
    import asyncio

    if not shutil.which("openssl"):
        pytest.skip("openssl required")
    rogue_key = tmp_path / "rogue.key"
    rogue_cert = tmp_path / "rogue.crt"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-keyout", str(rogue_key), "-out", str(rogue_cert), "-subj", "/CN=uqab-client",
        ],
        check=True,
        capture_output=True,
    )
    port = free_port()

    async def scenario() -> None:
        ctx = server_context(tls_settings)

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", port, ssl=ctx)
        rogue = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        rogue.minimum_version = ssl.TLSVersion.TLSv1_3
        rogue.load_verify_locations(cafile=tls_settings.ca)
        rogue.load_cert_chain(certfile=str(rogue_cert), keyfile=str(rogue_key))
        try:
            with pytest.raises((ssl.SSLError, OSError, ConnectionError, asyncio.IncompleteReadError)):
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        "127.0.0.1",
                        port,
                        ssl=rogue,
                        server_hostname=tls_settings.hostname,
                    ),
                    timeout=10,
                )
                writer.write(b"hello")
                await writer.drain()
                await asyncio.wait_for(reader.readexactly(1), timeout=5)
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())


def test_contexts_require_tls13(tls_settings: TLSSettings) -> None:
    client = client_context(tls_settings)
    server = server_context(tls_settings)
    assert client.minimum_version is ssl.TLSVersion.TLSv1_3
    assert server.minimum_version is ssl.TLSVersion.TLSv1_3
    assert server.verify_mode is ssl.CERT_REQUIRED
    assert client.verify_mode is ssl.CERT_REQUIRED
    assert client.check_hostname is True


def test_config_rejects_downgrade() -> None:
    cfg = load_config().with_overrides(**{"tls.min_version": "TLSv1_2"})
    with pytest.raises(TunnelError):
        TLSSettings.from_config(cfg)


def test_assert_tls13_rejects_plain_connection() -> None:
    class Plain:
        def get_extra_info(self, name: str, default: object = None) -> None:
            return None

    with pytest.raises(TunnelError):
        assert_tls13(Plain())


def test_certificate_material_is_complete(cert_dir: Path) -> None:
    paths = CertPaths(
        ca=cert_dir / "ca.crt",
        cert=cert_dir / "client.crt",
        key=cert_dir / "client.key",
        server_cert=cert_dir / "server.crt",
        server_key=cert_dir / "server.key",
    )
    assert paths.all_present()
    assert check(paths) == []


def test_generation_is_idempotent(cert_dir: Path) -> None:
    paths = CertPaths(
        ca=cert_dir / "ca.crt",
        cert=cert_dir / "client.crt",
        key=cert_dir / "client.key",
        server_cert=cert_dir / "server.crt",
        server_key=cert_dir / "server.key",
    )
    before = paths.ca.read_bytes()
    generate(paths)
    assert paths.ca.read_bytes() == before
