"""Fail-closed guard and tunnel state machine (FR-09, D9, SDD 7.2).

The rule this module enforces: when anything goes wrong, the upper connection
(Tor's SOCKS stream, or the bridge's ORPort stream) is closed and *nothing* is
sent outside the tunnel.  There is no direct fallback anywhere in the codebase,
which is what makes NFR-09 and NFR-16 hold by construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TunnelState(StrEnum):
    """The four states the app and the tunnel report (SRS 1.3, UC-6)."""

    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BLOCKED = "blocked"


class TunnelError(RuntimeError):
    """Any tunnel, frame or bridge failure; always handled by failing closed."""


class TunnelClosed(TunnelError):
    """The peer sent CLOSE or the stream ended mid-record."""


class SessionClosed(TunnelClosed):
    """The peer ended the session cleanly with a CLOSE frame (not a failure)."""


@dataclass(slots=True)
class FailClosedGuard:
    """Tracks failures, enforces the 10 s retry and closes the upper stream."""

    retry_s: float = 10.0
    state: TunnelState = TunnelState.STOPPED
    reason: str = ""
    blocked_at: float | None = None
    attempts: int = 0
    last_error_at: float | None = None
    _listeners: list[Any] = field(default_factory=list)

    def on_connecting(self) -> None:
        self.state = TunnelState.CONNECTING
        self.reason = ""

    def on_connected(self) -> None:
        self.state = TunnelState.CONNECTED
        self.reason = ""
        self.blocked_at = None
        self.attempts = 0

    def on_failure(self, reason: str) -> None:
        """Record a failure and move to Blocked (UC-6)."""
        self.attempts += 1
        self.last_error_at = time.time()
        self.state = TunnelState.BLOCKED
        self.reason = str(reason)[:500]
        if self.blocked_at is None:
            self.blocked_at = time.time()

    def on_stopped(self) -> None:
        self.state = TunnelState.STOPPED
        self.reason = ""
        self.blocked_at = None

    @property
    def blocked(self) -> bool:
        return self.state is TunnelState.BLOCKED

    async def retry_pause(self) -> None:
        """Wait the configured retry interval (FR-09: retry every 10 s)."""
        await asyncio.sleep(self.retry_s)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "attempts": self.attempts,
            "blocked_at": self.blocked_at,
        }


async def close_stream(writer: Any) -> None:
    """Close a stream immediately; abort() is used when the peer may be gone."""
    if writer is None:
        return
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
    except Exception:
        transport = getattr(writer, "transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.abort()


async def close_server(server: Any, writers: Any = (), *, wait_s: float = 2.0) -> None:
    """Stop listening and drop live connections without hanging.

    Since Python 3.12.1 ``Server.wait_closed()`` also waits for every accepted
    connection, so a listener with a live tunnel never finished closing. Live
    connections are aborted first and the wait is bounded.
    """
    if server is None:
        return
    server.close()
    for writer in list(writers):
        transport = getattr(writer, "transport", None)
        with contextlib.suppress(Exception):
            if transport is not None:
                transport.abort()
            else:
                writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(server.wait_closed(), timeout=wait_s)


async def fail_closed(
    guard: FailClosedGuard,
    reason: Exception | str,
    *writers: Any,
) -> None:
    """The single failure path: mark Blocked and tear down every attached stream."""
    guard.on_failure(str(reason))
    for writer in writers:
        await close_stream(writer)


def counters_snapshot(guard: FailClosedGuard) -> dict[str, Any]:
    """Convenience for the stats file writer."""
    return guard.snapshot()
