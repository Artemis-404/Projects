"""Mock bridge ORPort for lab mode.

In the real deployment the PT server forwards bytes to the bridge's Tor ORPort.
In lab mode this module plays that part: it speaks a tiny stream header so it
knows which destination the Tor stand-in asked for, and it **only** dials the
bundled demo origin.  Anything else is refused, so lab mode can never be used as
an open proxy and lab traffic can never reach the public internet.

Stream header (lab only, never used in the real path)::

    magic 8 B  b"UQABLAB1"
    atyp  1 B  1 = hostname
    length 2 B  big-endian host length
    host  N B   ASCII host name
    port  2 B   big-endian port
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

MAGIC = b"UQABLAB1"
ATYP_HOSTNAME = 1
MAX_HOST = 255


class StreamHeaderError(Exception):
    """Malformed or disallowed lab stream header."""


def encode_stream_header(host: str, port: int) -> bytes:
    raw = host.encode("idna")
    if not raw or len(raw) > MAX_HOST:
        raise StreamHeaderError("host name length out of range")
    return MAGIC + bytes([ATYP_HOSTNAME]) + len(raw).to_bytes(2, "big") + raw + port.to_bytes(2, "big")


async def read_stream_header(reader: asyncio.StreamReader) -> tuple[str, int]:
    magic = await reader.readexactly(len(MAGIC))
    if magic != MAGIC:
        raise StreamHeaderError("bad lab stream magic")
    atyp = (await reader.readexactly(1))[0]
    if atyp != ATYP_HOSTNAME:
        raise StreamHeaderError(f"unsupported lab address type {atyp}")
    length = int.from_bytes(await reader.readexactly(2), "big")
    if not 0 < length <= MAX_HOST:
        raise StreamHeaderError("host length out of range")
    host = (await reader.readexactly(length)).decode("ascii", "replace")
    port = int.from_bytes(await reader.readexactly(2), "big")
    return host, port


def resolve_lab_destination(host: str, port: int, origin_port: int, suffix: str = ".uqab") -> tuple[str, int] | None:
    """Map a demo host to the bundled origin, refusing anything else."""
    name = host.strip().lower().rstrip(".")
    if name == suffix.lstrip(".") or name.endswith(suffix):
        return "127.0.0.1", origin_port
    return None


class MockORPort:
    """Accepts PT-server byte streams and relays them to the demo origin."""

    def __init__(
        self,
        origin_port: int,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        suffix: str = ".uqab",
        log: logging.Logger | None = None,
    ) -> None:
        self.origin_port = origin_port
        self.host = host
        self.port = port
        self.suffix = suffix
        self.log = log or logging.getLogger("uqab.lab.orport")
        self.server: asyncio.Server | None = None
        self.refused = 0
        self.connections = 0

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, self.host, self.port)
        assert self.server.sockets
        self.port = int(self.server.sockets[0].getsockname()[1])
        self.log.info("mock ORPort on %s:%d (origin port %d)", self.host, self.port, self.origin_port)
        return self.port

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        upstream: asyncio.StreamWriter | None = None
        try:
            host, port = await asyncio.wait_for(read_stream_header(reader), timeout=15)
            target = resolve_lab_destination(host, port, self.origin_port, self.suffix)
            if target is None:
                self.refused += 1
                self.log.warning("lab ORPort refused destination %s:%d", host, port)
                return
            self.log.info("lab stream %s:%d -> demo origin", host, port)
            up_reader, upstream = await asyncio.open_connection(*target)
            # Both directions must run concurrently, or a response is only
            # forwarded after the request stream closes.
            await asyncio.gather(
                self._pipe(reader, upstream),
                self._pipe(up_reader, writer),
                return_exceptions=True,
            )
        except (StreamHeaderError, asyncio.IncompleteReadError, TimeoutError) as exc:
            self.log.debug("lab stream ended: %s", exc)
        except Exception as exc:  # pragma: no cover - diagnostics
            self.log.debug("lab stream error: %s", exc)
        finally:
            if upstream is not None:
                with contextlib.suppress(Exception):
                    upstream.close()
            with contextlib.suppress(Exception):
                writer.close()

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        total = 0
        try:
            while True:
                data = await reader.read(65_536)
                if not data:
                    break
                total += len(data)
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            logging.getLogger("uqab.lab.orport").debug("pipe finished after %d bytes", total)
            with contextlib.suppress(Exception):
                if writer.can_write_eof():
                    writer.write_eof()
