"""Managed pluggable transport client (FR-01, FR-09, SDD 5.4).

Run by Tor as ``ClientTransportPlugin uqab exec python -m pt.client``.  It speaks
the PT spec v1 managed handshake on stdout and gives Tor a SOCKS5 listener.

The mutual-TLS tunnel to the bridge is *persistent* (D21): it is opened on the
first SOCKS request, carries the session parameters once, and then keeps running
the persona timetable for as long as the client lives — including while the user
is idle — with every Tor stream multiplexed inside it.  Cover traffic therefore
never stops, and the observer never sees a "nothing at all" gap or a burst of N
parallel tunnels when a page opens N connections.

Failure behaviour (FR-09, D9): if the tunnel cannot be established or breaks, the
Tor SOCKS stream is closed and the client retries the bridge every
``cover.retry_s`` seconds.  There is no direct connection path anywhere in this
file.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Config, load_config
from cover.scheduler import Counters, SlotRecorder
from cover.timetable import session_seed
from logs import setup_logging
from persona.model import Persona
from persona.validator import Limits, discover_personas, fallback_persona, load_by_id
from pt import socks5
from pt.mux import MuxSession, Stream
from pt.session import SessionParams, client_handshake
from pt.stats import StatsWriter
from security.guard import (
    FailClosedGuard,
    TunnelError,
    close_server,
    close_stream,
    fail_closed,
)
from tunnel.certs import CertPaths, ensure
from tunnel.tls import TLSSettings, open_tunnel

VERSION_LINE = "VERSION 1"
METHOD = "uqab"
TRANSPORT_TYPE = "socks5"


@dataclass(frozen=True, slots=True)
class PTEnv:
    """The TOR_PT_* environment Tor hands to a managed client (SDD 5.4)."""

    managed_versions: str = ""
    transports: str = "uqab"
    state_location: str = ""
    exit_on_stdin_close: bool = False

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> PTEnv:
        return cls(
            managed_versions=environ.get("TOR_PT_MANAGED_TRANSPORT_VER", "1"),
            transports=environ.get("TOR_PT_CLIENT_TRANSPORTS", "uqab"),
            state_location=environ.get("TOR_PT_STATE_LOCATION", ""),
            exit_on_stdin_close=environ.get("TOR_PT_EXIT_ON_STDIN_CLOSE", "0") == "1",
        )

    def supports_v1(self) -> bool:
        offered = [part.strip() for part in self.managed_versions.split(",") if part.strip()]
        return "1" in offered


class ClientPT:
    """The UQAB managed client transport."""

    def __init__(
        self,
        cfg: Config | None = None,
        *,
        persona_id: str | None = None,
        log: Any = None,
        stats_path: Path | None = None,
        run_dir: Path | None = None,
    ) -> None:
        self.cfg = cfg or load_config()
        self.limits = Limits.from_config(self.cfg)
        self.log = log or setup_logging("pt-client")
        self.guard = FailClosedGuard(retry_s=self.cfg.get_float("cover.retry_s", 10.0))
        self.counters = Counters()
        self.greeting_size = self.cfg.get_int("cover.greeting_record_size", 1024)
        self.floor_slot_ms = self.cfg.get_int("cover.floor_slot_ms", 500)
        self.queue_bytes = self.cfg.get_int("cover.queue_bytes", 262_144)
        self.bind_host = self.cfg.get_str("pt.bind_host", "127.0.0.1")
        self.bind_port = self.cfg.get_int("pt.port", 0)
        self.connect_timeout = self.cfg.get_float("pt.connect_timeout_s", 60.0)
        self.retry_s = self.cfg.get_float("cover.retry_s", 10.0)
        self.tls = TLSSettings.from_config(self.cfg)
        self.bridge_host = self.cfg.get_str("bridge.host", "127.0.0.1")
        self.bridge_port = self.cfg.get_int("bridge.pt_port", 9443)

        run = run_dir or self.cfg.path("paths.run")
        self.run_dir = run
        run.mkdir(parents=True, exist_ok=True)
        (run / "logs").mkdir(parents=True, exist_ok=True)
        self.stats_path = stats_path or (run / "stats.json")
        self.persona_id = persona_id or self.cfg.get_str("persona.default", "balanced")
        self.period = ""
        self.session_started: float | None = None
        self.connected_peers = 0
        self.server: asyncio.Server | None = None
        self._active: set[asyncio.StreamWriter] = set()
        self.session: MuxSession | None = None
        self._session_lock = asyncio.Lock()
        self._recorder: SlotRecorder | None = None
        self._maintainer: asyncio.Task[Any] | None = None
        self._stopping = False
        self._slot_log = bool(self.cfg.get_bool("pt.log_slots", True))
        self.personas = self._load_personas()
        if self.persona_id not in self.personas:
            self.log.warning("persona %r unavailable; using balanced", self.persona_id)
            self.persona_id = "balanced"
        self.period = self.personas[self.persona_id].start_state
        self.stats = StatsWriter(
            self.stats_path,
            self.snapshot,
            interval=self.cfg.get_float("cover.stats_interval_s", 1.0),
        )
        self._exit = asyncio.Event()

    # ------------------------------------------------------------- personas
    def _load_personas(self) -> dict[str, Persona]:
        profiles = self.cfg.path("persona.profiles_dir", "persona/profiles")
        out: dict[str, Persona] = {}
        for summary in discover_personas(profiles, self.limits):
            if not summary.valid:
                self.log.warning("ignoring invalid persona %s: %s", summary.id, summary.errors)
                continue
            try:
                out[summary.id] = load_by_id(summary.id, profiles, self.limits)
            except Exception as exc:  # pragma: no cover - discovery already validated
                self.log.warning("cannot load persona %s: %s", summary.id, exc)
        out.setdefault("balanced", fallback_persona(self.limits))
        return out

    def persona_for(self, requested: str | None) -> Persona:
        if requested:
            persona = self.personas.get(requested)
            if persona is None:
                self.log.warning("unknown persona %r requested; using %s", requested, self.persona_id)
                return self.personas[self.persona_id]
            return persona
        return self.personas[self.persona_id]

    # ---------------------------------------------------------------- stats
    def snapshot(self) -> dict[str, Any]:
        data = {
            "t": time.time(),
            "state": self.guard.state.value,
            "persona": self.persona_id,
            "period": self.period or "",
            "real_up": self.counters.real_up,
            "real_down": self.counters.real_down,
            "cover_up": self.counters.cover_up,
            "cover_down": self.counters.cover_down,
            "overhead": self.counters.overhead,
            "bootstrap": 100 if self.guard.state.value == "connected" else 0,
            "session_started": self.session_started,
            "reason": self.guard.reason,
            "sessions": self.connected_peers,
            "bridge": f"{self.bridge_host}:{self.bridge_port}",
        }
        return data

    def publish(self) -> None:
        """Write the stats file now, so the app sees state changes immediately."""
        with contextlib.suppress(Exception):
            self.stats.write_once()

    # ------------------------------------------------------------ announcing
    def announce(self, port: int) -> None:
        """Print the managed-client handshake on stdout (FR-01)."""
        lines = [
            VERSION_LINE,
            f"CMETHOD {METHOD} {TRANSPORT_TYPE} {self.bind_host}:{port}",
            "CMETHODS DONE",
        ]
        for line in lines:
            print(line, flush=True)

    # ------------------------------------------------------------ connecting
    async def connect_bridge(self, persona: Persona) -> tuple[Any, Any]:
        """Open the TLS tunnel, retrying every ``retry_s`` until the deadline."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.connect_timeout
        attempt = 0
        while True:
            attempt += 1
            self.guard.on_connecting()
            try:
                reader, writer = await open_tunnel(
                    self.bridge_host, self.bridge_port, self.tls
                )
                self.log.info(
                    "tunnel established to %s:%d (attempt %d, persona %s)",
                    self.bridge_host, self.bridge_port, attempt, persona.id,
                )
                return reader, writer
            except TunnelError as exc:
                self.guard.on_failure(str(exc))
                self.publish()
                self.log.warning("bridge connect failed (attempt %d): %s", attempt, exc)
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(self.retry_s)

    # -------------------------------------------------------------- serving
    async def _handle_socks(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        self._active.add(writer)
        try:
            request, args = await asyncio.wait_for(socks5.accept(reader, writer), timeout=30)
        except (socks5.SocksError, TimeoutError) as exc:
            self.log.warning("SOCKS handshake from %s failed: %s", peer, exc)
            await close_stream(writer)
            return

        persona = self.persona_for(request.persona_id or args.get("persona"))
        self.log.info(
            "Tor stream from %s: %s:%d persona=%s", peer, request.host, request.port, persona.id
        )

        stream: Stream | None = None
        try:
            session = await self.carrier(persona)
            stream = await session.open(request.host, request.port, reader, writer)
        except TunnelError as exc:
            self.log.warning("cannot carry a stream: %s", exc)
            with contextlib.suppress(Exception):
                await socks5.send_reply(writer, socks5.REP_GENERAL_FAILURE)
            await close_stream(writer)
            self._active.discard(writer)
            return

        with contextlib.suppress(Exception):
            await socks5.send_reply(writer, socks5.REP_SUCCESS)
        self.connected_peers = len(self.session.streams) if self.session else 0
        self.publish()
        try:
            await stream.done.wait()
        finally:
            if stream.error:
                self.log.warning("stream %d ended with %s", stream.sid, stream.error)
            self.connected_peers = len(self.session.streams) if self.session else 0
            self.publish()
            await close_stream(writer)
            self._active.discard(writer)

    # --------------------------------------------------------------- carrier
    async def carrier(self, persona: Persona) -> MuxSession:
        """Return the live shaped session, opening it on first use (D21).

        The session is shared by every SOCKS stream and outlives them: after the
        last stream closes the slot clock keeps running, so the record stream an
        observer sees is continuous for the whole lifetime of the client.
        """
        async with self._session_lock:
            session = self.session
            if session is not None and session.is_open:
                if session.persona.id != persona.id:
                    self.log.warning(
                        "persona %s requested but the session runs %s; "
                        "Tor fixes the persona per session",
                        persona.id,
                        session.persona.id,
                    )
                return session
            tls_reader, tls_writer = await self.connect_bridge(persona)
            seed = session_seed()
            params = SessionParams(
                persona=persona.id, sha256=persona.content_hash(), seed=seed
            )
            try:
                await client_handshake(tls_reader, tls_writer, params, self.greeting_size)
            except Exception as exc:
                await fail_closed(self.guard, exc, None, tls_writer)
                raise TunnelError(f"session start rejected: {exc}") from exc

            recorder = None
            if self._slot_log:
                recorder = SlotRecorder(self.run_dir / "logs" / "slots-client.jsonl")
            session = MuxSession(
                tls_reader,
                tls_writer,
                persona=persona,
                seed=seed,
                counters=self.counters,
                floor_slot_ms=self.floor_slot_ms,
                queue_bytes=self.queue_bytes,
                recorder=recorder,
                on_period=self._set_period,
                on_ping=self._on_ping,
                log=self.log,
            )
            await session.start()
            self.session = session
            self._recorder = recorder
            self.persona_id = persona.id
            self.period = persona.start_state
            self.session_started = time.time()
            self.guard.on_connected()
            self.publish()
            self.log.info(
                "shaped session up: persona=%s seed=%d (cover runs until shutdown)",
                persona.id,
                seed,
            )
            asyncio.create_task(self._watch_session(session), name="uqab-session-watch")
            return session

    async def _watch_session(self, session: MuxSession) -> None:
        """Publish the failure and release the slot clock when a session dies."""
        await session.wait_closed()
        if self.session is not session:
            return
        self.session = None
        self.connected_peers = 0
        if self._recorder is not None:
            self.log.info("slot timing: %s", self._recorder.summary())
            self._recorder.close()
            self._recorder = None
        if session.error is not None:
            self.guard.on_failure(str(session.error))
        else:
            self.guard.on_stopped()
        self.publish()
        self.log.info("shaped session closed")

    def _on_ping(self, payload: dict[str, object]) -> None:
        self.log.debug("session ping: %s", payload)

    def _set_period(self, period: str) -> None:
        self.period = period
        self.log.debug("persona period -> %s", period)
        self.publish()

    async def serve(self, *, announce: bool = True) -> None:
        """Start the SOCKS listener, the cover tunnel and the stats writer.

        The shaped session is opened *eagerly* (D21): the app's tab gate only
        opens once the tunnel reports ``connected``, and the whole point of the
        persistent session is that cover starts with the engine, not with the
        first page.
        """
        self.stats.start()
        self._maintainer = asyncio.create_task(self._maintain_carrier(), name="uqab-carrier")
        self.server = await asyncio.start_server(
            self._handle_socks, self.bind_host, self.bind_port
        )
        socket = self.server.sockets[0] if self.server.sockets else None
        port = socket.getsockname()[1] if socket is not None else self.bind_port
        if announce:
            self.announce(port)
        self.log.info("PT client listening on %s:%d", self.bind_host, port)
        try:
            # Already accepting; see ServerPT.serve for why not serve_forever().
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            pass
        finally:
            self.server.close()
            await self.stats.stop()

    async def _maintain_carrier(self) -> None:
        """Keep a shaped session up for as long as the client runs (D21, FR-09)."""
        while not self._stopping:
            session = self.session
            if session is not None and session.is_open:
                await session.wait_closed()
                continue
            persona = self.personas[self.persona_id]
            try:
                session = await self.carrier(persona)
            except TunnelError as exc:
                self.log.warning("cover tunnel unavailable (%s); retrying in %.0fs", exc, self.retry_s)
                self.guard.on_failure(str(exc))
                self.publish()
                await asyncio.sleep(self.retry_s)
                continue
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                return
            except Exception as exc:  # pragma: no cover - defensive
                self.log.exception("unexpected carrier failure: %s", exc)
                await asyncio.sleep(self.retry_s)
                continue
            await session.wait_closed()

    async def shutdown(self) -> None:
        """Stop accepting, end the shaped session and close every Tor stream."""
        self._stopping = True
        if self._maintainer is not None:
            self._maintainer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._maintainer
            self._maintainer = None
        if self.session is not None:
            await self.session.close("client shutting down")
            self.session = None
        if self._recorder is not None:
            self._recorder.close()
            self._recorder = None
        self.guard.on_stopped()
        self.stats.write_once()
        await self.stats.stop()
        for stream_writer in list(self._active):
            await close_stream(stream_writer)
        await close_server(self.server, self._active)
        self._active.clear()


def validate_managed_env(env: PTEnv) -> None:
    """Refuse to run for a Tor version we do not implement (PT spec v1)."""
    if not env.supports_v1():
        print(
            f"uqab: Tor offered managed transport version(s) {env.managed_versions!r}; "
            "UQAB implements version 1 only",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    """Entry point used by Tor (``python -m pt.client``) and by developers."""
    import argparse

    parser = argparse.ArgumentParser(description="UQAB PT client")
    parser.add_argument("--persona", default=None, help="persona id for direct runs")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--bridge-host", default=None)
    parser.add_argument("--bridge-port", type=int, default=None)
    parser.add_argument("--bind-port", type=int, default=None)
    parser.add_argument("--no-announce", action="store_true", help="do not print CMETHOD lines")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    overrides: dict[str, Any] = {}
    if args.bridge_host:
        overrides["bridge.host"] = args.bridge_host
    if args.bridge_port:
        overrides["bridge.pt_port"] = args.bridge_port
    if args.bind_port is not None:
        overrides["pt.port"] = args.bind_port
    if overrides:
        cfg = cfg.with_overrides(**overrides)

    cfg.ensure_dirs()
    log = setup_logging(
        "pt-client",
        cfg.path("paths.run") / "logs",
        level=10 if args.debug else 20,
    )
    env = PTEnv.from_environ(os.environ)
    if env.managed_versions:
        validate_managed_env(env)
    ensure(CertPaths.from_config(cfg))
    client = ClientPT(cfg, persona_id=args.persona, log=log)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def runner() -> None:
        task = asyncio.create_task(client.serve(announce=not args.no_announce))
        stop = asyncio.Event()

        def request_stop() -> None:
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, request_stop)
        if env.exit_on_stdin_close:
            loop.run_in_executor(None, _wait_for_stdin_close, request_stop)
        await stop.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await client.shutdown()

    try:
        loop.run_until_complete(runner())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        loop.close()
    return 0


def _wait_for_stdin_close(callback: Any) -> None:
    """Tor closes our stdin when it wants the PT to exit."""
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except Exception:
        pass
    with contextlib.suppress(Exception):
        callback()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
