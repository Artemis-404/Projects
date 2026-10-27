"""Lab simulation tests: mock Tor, mock ORPort, demo origin (lab mode only)."""

from __future__ import annotations

import pytest

from config import Config
from experiments.socks_http import HttpError, fetch
from lab.mocktor import parse_cmethod
from lab.orport import (
    MAGIC,
    StreamHeaderError,
    encode_stream_header,
    read_stream_header,
    resolve_lab_destination,
)
from lab.stack import LabStack
from tests.conftest import run
from tests.support import FakeReader


def test_stream_header_roundtrip() -> None:
    raw = encode_stream_header("demo.uqab", 80)
    assert raw.startswith(MAGIC)

    async def scenario() -> tuple[str, int]:
        return await read_stream_header(FakeReader(raw))  # type: ignore[arg-type]

    assert run(scenario()) == ("demo.uqab", 80)


def test_stream_header_rejects_bad_magic() -> None:
    async def scenario() -> None:
        await read_stream_header(FakeReader(b"NOTUQAB1" + b"\x01\x00\x03abc\x00\x50"))  # type: ignore[arg-type]

    with pytest.raises(StreamHeaderError):
        run(scenario())


@pytest.mark.parametrize(
    "host, allowed",
    [
        ("demo.uqab", True),
        ("anything.uqab", True),
        ("uqab", True),
        ("example.com", False),
        ("127.0.0.1", False),
        ("demo.uqab.evil.com", False),
    ],
)
def test_lab_destination_is_restricted_to_the_demo_origin(host: str, allowed: bool) -> None:
    result = resolve_lab_destination(host, 80, 8899)
    assert (result is not None) is allowed
    if allowed:
        assert result == ("127.0.0.1", 8899)


def test_cmethod_parsing() -> None:
    method = parse_cmethod("CMETHOD uqab socks5 127.0.0.1:40231")
    assert method is not None
    assert (method.name, method.kind, method.host, method.port) == ("uqab", "socks5", "127.0.0.1", 40231)
    assert parse_cmethod("CMETHODS DONE") is None
    assert parse_cmethod("VERSION 1") is None


def test_lab_stack_serves_pages_through_the_shaped_tunnel(cfg: Config) -> None:
    async def scenario() -> None:
        stack = LabStack(
            cfg, persona="high", socks_port=0, bridge_pt_port=0, origin_port=0
        )
        info = await stack.start()
        try:
            assert info["ready"] is True
            assert info["mode"] == "simulated"
            load, body = await fetch(
                "http://demo.uqab/", "127.0.0.1", info["socks_port"], timeout=20
            )
            assert load.status == 200
            assert b"UQAB lab origin" in body
            assert load.bytes_received > 1000

            load2, body2 = await fetch(
                "http://demo.uqab/docs", "127.0.0.1", info["socks_port"], timeout=20
            )
            assert load2.status == 200
            assert b"Frame format" in body2

            # Anything outside the demo origin must be refused by the lab ORPort.
            with pytest.raises((HttpError, Exception)):
                await fetch("http://example.com/", "127.0.0.1", info["socks_port"], timeout=10)
        finally:
            await stack.stop()

    run(scenario(), timeout=180)
