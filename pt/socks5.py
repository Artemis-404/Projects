"""Minimal SOCKS5 server used by the PT client (FR-01, PT spec v1).

Tor connects to the address the PT prints in ``CMETHOD`` and speaks SOCKS5.  When
the bridge line carries arguments (``persona=balanced``) Tor selects the
username/password method and puts the arguments in the username field, as the
pluggable transport specification defines.  This module implements just enough
of RFC 1928 and RFC 1929 for that channel, with no third-party code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

SOCKS_VERSION = 5
AUTH_VERSION = 1
METHOD_NO_AUTH = 0x00
METHOD_USERPASS = 0x02
METHOD_NONE_ACCEPTABLE = 0xFF

CMD_CONNECT = 0x01

REP_SUCCESS = 0x00
REP_GENERAL_FAILURE = 0x01
REP_NOT_ALLOWED = 0x02
REP_HOST_UNREACHABLE = 0x04
REP_CMD_NOT_SUPPORTED = 0x07
REP_ATYP_NOT_SUPPORTED = 0x08

ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04


class SocksError(Exception):
    """Malformed SOCKS handshake."""


@dataclass(frozen=True, slots=True)
class SocksRequest:
    """A parsed SOCKS5 CONNECT request plus the PT client arguments."""

    host: str
    port: int
    args: dict[str, str]
    command: int = CMD_CONNECT

    @property
    def persona_id(self) -> str | None:
        return self.args.get("persona")


def parse_args(raw: str) -> dict[str, str]:
    """Parse ``persona=balanced,mode=x`` into a mapping (PT spec client args)."""
    result: dict[str, str] = {}
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


async def _read_exact(reader: asyncio.StreamReader, count: int) -> bytes:
    try:
        return await reader.readexactly(count)
    except asyncio.IncompleteReadError as exc:
        raise SocksError("client closed during the SOCKS handshake") from exc


async def negotiate_auth(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> dict[str, str]:
    """Perform the method negotiation (and optional username/password auth)."""
    header = await _read_exact(reader, 2)
    if header[0] != SOCKS_VERSION:
        raise SocksError(f"unsupported SOCKS version {header[0]}")
    method_count = header[1]
    if method_count == 0:
        raise SocksError("client offered no auth methods")
    methods = await _read_exact(reader, method_count)
    if METHOD_USERPASS in methods:
        writer.write(bytes([SOCKS_VERSION, METHOD_USERPASS]))
        await writer.drain()
        version = await _read_exact(reader, 1)
        if version[0] != AUTH_VERSION:
            raise SocksError(f"unsupported auth version {version[0]}")
        ulen = (await _read_exact(reader, 1))[0]
        username = (await _read_exact(reader, ulen)).decode("utf-8", "replace")
        plen = (await _read_exact(reader, 1))[0]
        password = (await _read_exact(reader, plen)).decode("utf-8", "replace")
        writer.write(bytes([AUTH_VERSION, 0x00]))
        await writer.drain()
        args = parse_args(username)
        # Some Tor builds put the arguments in the password field instead.
        if not args and password:
            args = parse_args(password)
        return args
    if METHOD_NO_AUTH in methods:
        writer.write(bytes([SOCKS_VERSION, METHOD_NO_AUTH]))
        await writer.drain()
        return {}
    writer.write(bytes([SOCKS_VERSION, METHOD_NONE_ACCEPTABLE]))
    await writer.drain()
    raise SocksError("no acceptable auth method")


async def read_request(reader: asyncio.StreamReader) -> SocksRequest:
    """Read a SOCKS5 CONNECT request and return its destination and arguments."""
    ver_cmd_rsv = await _read_exact(reader, 3)
    version, command, _reserved = ver_cmd_rsv
    if version != SOCKS_VERSION:
        raise SocksError(f"unsupported SOCKS version {version}")
    atyp = await _read_exact(reader, 1)
    kind = atyp[0]
    if kind == ATYP_IPV4:
        host = ".".join(str(b) for b in await _read_exact(reader, 4))
    elif kind == ATYP_IPV6:
        raw = await _read_exact(reader, 16)
        host = ":".join(f"{raw[i]:02x}{raw[i + 1]:02x}" for i in range(0, 16, 2))
    elif kind == ATYP_DOMAIN:
        length = (await _read_exact(reader, 1))[0]
        host = (await _read_exact(reader, length)).decode("idna", "replace")
    else:
        raise SocksError(f"unsupported address type {kind}")
    port = int.from_bytes(await _read_exact(reader, 2), "big")
    return SocksRequest(host=host, port=port, args={}, command=command)


def reply_bytes(code: int = REP_SUCCESS) -> bytes:
    """SOCKS5 reply with BND.ADDR 0.0.0.0 and BND.PORT 0."""
    return bytes([SOCKS_VERSION, code, 0x00, ATYP_IPV4, 0, 0, 0, 0, 0, 0])


async def send_reply(writer: asyncio.StreamWriter, code: int = REP_SUCCESS) -> None:
    writer.write(reply_bytes(code))
    await writer.drain()


async def accept(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> tuple[SocksRequest, dict[str, str]]:
    """Full handshake: negotiation, then the CONNECT request."""
    args = await negotiate_auth(reader, writer)
    request = await read_request(reader)
    if request.command != CMD_CONNECT:
        await send_reply(writer, REP_CMD_NOT_SUPPORTED)
        raise SocksError(f"unsupported SOCKS command {request.command}")
    return request, args
