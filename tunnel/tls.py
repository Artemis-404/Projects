"""Mutual TLS 1.3 tunnel (FR-03, NFR-07, D2).

Only Python's :mod:`ssl` module is used; there is no custom cryptography.  Both
sides must present a certificate signed by the UQAB lab CA and the negotiated
protocol must be TLS 1.3, otherwise the connection is rejected before a single
record is exchanged.
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from typing import Any

from config import Config
from tunnel.certs import CertPaths

TLS13 = ssl.TLSVersion.TLSv1_3


class TunnelError(RuntimeError):
    """TLS setup or handshake failure; callers must fail closed (FR-09)."""


@dataclass(frozen=True, slots=True)
class TLSSettings:
    """Everything the tunnel needs, taken from ``config/config.yaml``."""

    ca: str
    cert: str
    key: str
    server_cert: str
    server_key: str
    hostname: str
    min_version: ssl.TLSVersion = TLS13

    @classmethod
    def from_config(cls, cfg: Config) -> TLSSettings:
        paths = CertPaths.from_config(cfg)
        version = TLS13
        configured = cfg.get_str("tls.min_version", "TLSv1_3")
        if configured not in {"TLSv1_3", "TLSv1.3", "tlsv1_3"}:
            raise TunnelError(f"tls.min_version {configured!r} is not allowed; TLS 1.3 only")
        return cls(
            ca=str(paths.ca),
            cert=str(paths.cert),
            key=str(paths.key),
            server_cert=str(paths.server_cert),
            server_key=str(paths.server_key),
            hostname=cfg.get_str("tls.hostname", "uqab-bridge"),
            min_version=version,
        )


def _base_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = TLS13
    ctx.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_TLSv1_2
    ctx.options |= ssl.OP_NO_COMPRESSION
    return ctx


def client_context(settings: TLSSettings) -> ssl.SSLContext:
    """Context for the PT client: verifies the bridge and presents its own cert."""
    ctx = _base_context()
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    try:
        ctx.load_verify_locations(cafile=settings.ca)
        ctx.load_cert_chain(certfile=settings.cert, keyfile=settings.key)
    except (OSError, ssl.SSLError) as exc:
        raise TunnelError(f"cannot load client TLS material: {exc}") from exc
    return ctx


def server_context(settings: TLSSettings) -> ssl.SSLContext:
    """Context for the PT server: requires a client certificate signed by the CA."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = TLS13
    ctx.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = False
    try:
        ctx.load_verify_locations(cafile=settings.ca)
        ctx.load_cert_chain(certfile=settings.server_cert, keyfile=settings.server_key)
    except (OSError, ssl.SSLError) as exc:
        raise TunnelError(f"cannot load server TLS material: {exc}") from exc
    return ctx


def assert_tls13(writer: Any) -> None:
    """Refuse any session that did not negotiate TLS 1.3."""
    ssl_object = None
    getter = getattr(writer, "get_extra_info", None)
    if callable(getter):
        try:
            ssl_object = getter("ssl_object", None)
        except TypeError:  # pragma: no cover - stream doubles with a strict signature
            ssl_object = getter("ssl_object")
    if ssl_object is None:
        raise TunnelError("connection is not TLS")
    version = ssl_object.version()
    if version != "TLSv1.3":
        raise TunnelError(f"negotiated {version}; TLS 1.3 is required")


async def open_tunnel(
    host: str,
    port: int,
    settings: TLSSettings,
    *,
    limit_s: float = 20.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a mutually authenticated TLS 1.3 connection to the bridge."""
    try:
        async with asyncio.timeout(limit_s):
            reader, writer = await asyncio.open_connection(
                host=host,
                port=port,
                ssl=client_context(settings),
                server_hostname=settings.hostname,
                ssl_handshake_timeout=limit_s,
            )
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        raise TunnelError(f"TLS connect to {host}:{port} failed: {exc}") from exc
    assert_tls13(writer)
    return reader, writer


def peer_subject(writer: Any) -> str:
    """Best-effort subject of the peer certificate, for logs."""
    try:
        cert = writer.get_extra_info("peercert") or {}
        parts = [f"{k}={v}" for rdn in cert.get("subject", ()) for k, v in rdn]
        return ", ".join(parts)
    except Exception:  # pragma: no cover - diagnostics only
        return "unknown"
