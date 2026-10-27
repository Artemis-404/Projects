"""Shared test doubles: fake streams, a mock ORPort and a Tor-like SOCKS caller."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any


class FakeWriter:
    """Minimal StreamWriter stand-in that records every write call."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default

    @property
    def payload(self) -> bytes:
        return b"".join(self.writes)


class FakeReader:
    """StreamReader stand-in fed with fixed bytes."""

    def __init__(self, data: bytes = b"") -> None:
        self._buffer = bytearray(data)

    def feed(self, data: bytes) -> None:
        self._buffer += data

    async def readexactly(self, count: int) -> bytes:
        if len(self._buffer) < count:
            partial = bytes(self._buffer)
            self._buffer.clear()
            raise asyncio.IncompleteReadError(partial, count)
        out = bytes(self._buffer[:count])
        del self._buffer[:count]
        return out

    async def read(self, count: int) -> bytes:
        if not self._buffer:
            return b""
        out = bytes(self._buffer[:count])
        del self._buffer[:count]
        return out


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def open_echo_server(port: int = 0) -> tuple[asyncio.Server, int]:
    """An ORPort stand-in that echoes every byte back."""

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65_536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", port)
    actual = server.sockets[0].getsockname()[1]
    return server, int(actual)


async def open_null_server(port: int = 0) -> tuple[asyncio.Server, int]:
    """An ORPort stand-in that accepts and discards, recording byte counts."""

    state = {"bytes": 0}

    async def sink(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65_536)
                if not data:
                    break
                state["bytes"] += len(data)
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    server = await asyncio.start_server(sink, "127.0.0.1", port)
    actual = server.sockets[0].getsockname()[1]
    server.state = state  # type: ignore[attr-defined]
    return server, int(actual)


class SocksCaller:
    """Acts like Tor: SOCKS5 with client args, then raw bytes."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(
        cls,
        port: int,
        args: str = "persona=balanced",
        host: str = "127.0.0.1",
        use_auth: bool = True,
    ) -> SocksCaller:
        reader, writer = await asyncio.open_connection(host, port)
        if use_auth:
            writer.write(b"\x05\x01\x02")
        else:
            writer.write(b"\x05\x01\x00")
        await writer.drain()
        choice = await reader.readexactly(2)
        assert choice[0] == 5, choice
        if choice[1] == 0x02:
            username = args.encode()
            writer.write(bytes([0x01, len(username)]) + username + b"\x00")
        await writer.drain()
        if use_auth:
            reply = await reader.readexactly(2)
            assert reply == b"\x01\x00", reply
        request = b"\x05\x01\x00\x01" + bytes([127, 0, 0, 1]) + (80).to_bytes(2, "big")
        writer.write(request)
        await writer.drain()
        head = await reader.readexactly(4)
        assert head[0] == 5, head
        await reader.readexactly(6)
        return cls(reader, writer)

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def recv(self, count: int, timeout: float = 5.0) -> bytes:  # noqa: ASYNC109
        return await asyncio.wait_for(self.reader.readexactly(count), timeout=timeout)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()
