"""A small SOCKS5 HTTP client used by the experiment driver (FR-20, FR-21).

Playwright is the primary driver, but the evaluation pipeline must also run on a
machine where it is not installed.  This module speaks SOCKS5 with **remote DNS**
(the host name is sent to the proxy, not resolved locally) and performs one HTTP
GET with ``Connection: close``, which is enough for the scripted ACTIVE/IDLE
timeline and for page-load timing (NFR-01).
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

USER_AGENT = "UQAB-Experiment/1.0"


class HttpError(RuntimeError):
    """The page could not be loaded through the proxy."""


@dataclass(frozen=True, slots=True)
class PageLoad:
    url: str
    status: int
    bytes_received: int
    load_ms: float


async def _socks_connect(
    proxy_host: str, proxy_port: int, host: str, port: int, timeout: float  # noqa: ASYNC109
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(proxy_host, proxy_port), timeout=timeout
    )
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        choice = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
        if choice[0] != 5 or choice[1] != 0:
            raise HttpError(f"proxy rejected SOCKS negotiation: {choice!r}")
        raw = host.encode("idna")
        writer.write(
            b"\x05\x01\x00\x03" + bytes([len(raw)]) + raw + port.to_bytes(2, "big")
        )
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        if reply[0] != 5:
            raise HttpError("bad SOCKS reply version")
        if reply[1] != 0:
            raise HttpError(f"SOCKS CONNECT failed with code {reply[1]}")
        atyp = reply[3]
        if atyp == 1:
            await reader.readexactly(6)
        elif atyp == 4:
            await reader.readexactly(18)
        else:
            length = (await reader.readexactly(1))[0]
            await reader.readexactly(length + 2)
        return reader, writer
    except Exception:
        writer.close()
        raise


async def fetch(
    url: str,
    proxy_host: str,
    proxy_port: int,
    *,
    timeout: float = 30.0,  # noqa: ASYNC109 - wall-clock limit for the whole load
    user_agent: str = USER_AGENT,
) -> tuple[PageLoad, bytes]:
    """Fetch ``url`` through the SOCKS5 proxy and return the load plus the body."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise HttpError(f"unsupported scheme {parts.scheme!r}")
    host = parts.hostname or ""
    if not host:
        raise HttpError(f"no host in {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    started = time.perf_counter()
    reader, writer = await _socks_connect(proxy_host, proxy_port, host, port, timeout)
    try:
        if parts.scheme == "https":
            ctx = ssl.create_default_context()
            await writer.start_tls(ctx, server_hostname=host)
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: {user_agent}\r\n"
            "Accept: text/html,application/xhtml+xml\r\n"
            "Accept-Encoding: identity\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(request.encode("ascii", "replace"))
        await writer.drain()
        body = bytearray()
        while True:
            chunk = await asyncio.wait_for(reader.read(65_536), timeout=timeout)
            if not chunk:
                break
            body += chunk
        elapsed_ms = (time.perf_counter() - started) * 1000.0
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=2)

    status = _parse_status(bytes(body))
    if not body or status == 0:
        raise HttpError(f"no HTTP response from {url} (connection closed by the proxy)")
    return PageLoad(url=url, status=status, bytes_received=len(body), load_ms=elapsed_ms), bytes(body)


def _parse_status(response: bytes) -> int:
    try:
        head = response.split(b"\r\n", 1)[0].decode("ascii", "replace")
        return int(head.split()[1])
    except (IndexError, ValueError):
        return 0


def fetch_sync(
    url: str, proxy_host: str, proxy_port: int, *, timeout: float = 30.0
) -> PageLoad:
    """Blocking wrapper used by the Playwright-less driver and by scripts."""

    async def run() -> PageLoad:
        load, _ = await fetch(url, proxy_host, proxy_port, timeout=timeout)
        return load

    return asyncio.run(run())
