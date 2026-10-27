"""Managed pluggable transport server (FR-02, SDD 5.4).

Run on the bridge as ``ServerTransportPlugin uqab exec python -m pt.server``.  It
prints the managed-server handshake, terminates the mutual-TLS tunnel, validates
the session parameters against the personas it holds, and forwards recovered
real bytes to the bridge's Tor ORPort.  The return direction is shaped on the
same timetable as the client (FR-07).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Config, load_config
from cover.scheduler import Counters, SlotRecorder
from logs import setup_logging
from persona.model import Persona
from persona.validator import Limits, discover_personas, fallback_persona, load_by_id
from pt.session import PTStream, server_handshake
from security.guard import TunnelError, close_stream
from tunnel.certs import CertPaths, ensure
from tunnel.tls import TLSSettings, assert_tls13, peer_subject, server_context

VERSION_LINE = "VERSION 1"
METHOD = "uqab"


def split_hostport(value: str) -> tuple[str, int]:
    """Split ``host:port`` or ``[v6]:port``."""
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]:")
        return host, int(rest)
    host, _, port = value.rpartition(":")
    if not host or not port:
        raise ValueError(f"cannot parse address {value!r}")
    return host, int(port)


@dataclass(frozen=True, slots=True)
class ServerEnv:
    """The TOR_PT_* environment Tor hands to a managed server (SDD 5.4)."""

    transports: tuple[str, ...] = (METHOD,)
    bind_addrs: dict[str, tuple[str, int]] = field(default_factory=dict)
    orport: tuple[str, int] = ("127.0.0.1", 9001)
    state_location: str = ""
    exit_on_stdin_close: bool = False

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> ServerEnv:
        transports = tuple(
            part.strip()
            for part in environ.get("TOR_PT_SERVER_TRANSPORTS", METHOD).split(",")
            if part.strip()
        )
        binds: dict[str, tuple[str, int]] = {}
        raw_bind = environ.get("TOR_PT_SERVER_BINDADDR", "")
        for entry in raw_bind.split(","):
            entry = entry.strip()
            if not entry:
                continue
            transport, _, address = entry.partition("-")
            try:
                binds[transport] = split_hostport(address)
            except ValueError:
                continue
        orport_raw = environ.get("TOR_PT_ORPORT", "127.0.0.1:9001")
        try:
            orport = split_hostport(orport_raw)
        except ValueError:
            orport = ("127.0.0.1", 9001)
        return cls(
            transports=transports or (METHOD,),
            bind_addrs=binds,
            orport=orport,
            state_location=environ.get("TOR_PT_STATE_LOCATION", ""),
            exit_on_stdin_close=environ.get("TOR_PT_EXIT_ON_STDIN_CLOSE", "0") == "1",
        )


class ServerPT:
    """The UQAB bridge transport."""

    def __init__(
        self,
        cfg: Config | None = None,
        *,
        orport: tuple[str, int] | None = None,
        bind: tuple[str, int] | None = None,
        log: Any = None,
        run_dir: Path | None = None,
    ) -> None:
        self.cfg = cfg or load_config()
        self.limits = Limits.from_config(self.cfg)
        self.log = log or setup_logging("pt-server")
        self.tls = TLSSettings.from_config(self.cfg)
        self.orport = orport or (
            self.cfg.get_str("bridge.orport_host", "127.0.0.1"),
            self.cfg.get_int("bridge.orport_port", 9001),
        )
        self.bind = bind or (
            self.cfg.get_str("pt.server_bind_host", "0.0.0.0"),
            self.cfg.get_int("bridge.pt_port", 9443),
        )
        self.greeting_size = self.cfg.get_int("cover.greeting_record_size", 1024)
        self.floor_slot_ms = self.cfg.get_int("cover.floor_slot_ms", 500)
        self.queue_bytes = self.cfg.get_int("cover.queue_bytes", 262_144)
        self.run_dir = run_dir or self.cfg.path("paths.run")
        (self.run_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.personas = self._load_personas()
        self.counters = Counters()
        self.sessions = 0
        self.server: asyncio.Server | None = None
        self._active: set[asyncio.StreamWriter] = set()
        self._log_slots = self.cfg.get_bool("pt.log_slots", True)
        self._stop = asyncio.Event()

    def _load_personas(self) -> dict[str, Persona]:
        profiles = self.cfg.path("persona.profiles_dir", "persona/profiles")
        out: dict[str, Persona] = {}
        for summary in discover_personas(profiles, self.limits):
            if not summary.valid:
                self.log.warning("bridge ignores invalid persona %s: %s", summary.id, summary.errors)
                continue
            try:
                out[summary.id] = load_by_id(summary.id, profiles, self.limits)
            except Exception as exc:  # pragma: no cover
                self.log.warning("cannot load persona %s: %s", summary.id, exc)
        out.setdefault("balanced", fallback_persona(self.limits))
        return out

    def announce(self, host: str, port: int) -> None:
        shown = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        for line in (VERSION_LINE, f"SMETHOD {METHOD} {shown}", "SMETHODS DONE"):
            print(line, flush=True)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            assert_tls13(writer)
        except TunnelError as exc:
            self.log.warning("rejecting non-TLS 1.3 connection: %s", exc)
            await close_stream(writer)
            return
        self._active.add(writer)
        self.log.info("incoming tunnel from %s (%s)", writer.get_extra_info("peername"), peer_subject(writer))

        try:
            or_reader, or_writer = await asyncio.open_connection(*self.orport)
        except OSError as exc:
            self.log.error("ORPort %s unreachable: %s", self.orport, exc)
            await close_stream(writer)
            return

        try:
            params, persona = await server_handshake(
                reader, writer, self.personas, self.greeting_size, self.log
            )
        except TunnelError as exc:
            self.log.warning("session rejected: %s", exc)
            await close_stream(writer)
            await close_stream(or_writer)
            return
        except Exception as exc:  # pragma: no cover - malformed input
            self.log.warning("session failed: %s", exc)
            await close_stream(writer)
            await close_stream(or_writer)
            return

        self.sessions += 1
        session_counters = Counters()
        recorder = None
        if self._log_slots:
            recorder = SlotRecorder(self.run_dir / "logs" / "slots-server.jsonl")
        stream = PTStream(
            reader,
            writer,
            persona=persona,
            seed=params.seed,
            counters=session_counters,
            upper_reader=or_reader,
            upper_writer=or_writer,
            floor_slot_ms=self.floor_slot_ms,
            queue_bytes=self.queue_bytes,
            recorder=recorder,
            log=self.log,
        )
        started = time.time()
        try:
            await stream.run()
            self.log.info("session finished cleanly after %.1fs", time.time() - started)
        except TunnelError as exc:
            self.log.warning("tunnel failure on bridge side: %s", exc)
        except Exception:
            self.log.exception("unexpected bridge session error")
        finally:
            self._accumulate(session_counters)
            if recorder is not None:
                self.log.info("bridge slot timing: %s", recorder.summary())
                recorder.close()
            await stream.close()
            await close_stream(or_writer)
            self._active.discard(writer)

    def _accumulate(self, counters: Counters) -> None:
        self.counters.real_up += counters.real_up
        self.counters.real_down += counters.real_down
        self.counters.cover_up += counters.cover_up
        self.counters.cover_down += counters.cover_down

    async def serve(self, *, announce: bool = True) -> None:
        ctx = server_context(self.tls)
        self.server = await asyncio.start_server(
            self._handle,
            self.bind[0],
            self.bind[1],
            ssl=ctx,
            ssl_handshake_timeout=20.0,
        )
        socket = self.server.sockets[0] if self.server.sockets else None
        host, port = socket.getsockname()[:2] if socket is not None else self.bind
        if announce:
            self.announce(str(host), int(port))
        self.log.info(
            "PT server listening on %s:%s (ORPort %s:%s)",
            host, port, self.orport[0], self.orport[1],
        )
        async with self.server:
            await self.server.serve_forever()

    async def shutdown(self) -> None:
        """Stop listening and abort every live tunnel (fail closed on the client)."""
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
        for writer in list(self._active):
            transport = getattr(writer, "transport", None)
            with contextlib.suppress(Exception):
                if transport is not None:
                    transport.abort()
                else:
                    writer.close()
        self._active.clear()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="UQAB PT server (bridge side)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--bind", default=None, help="listen address, e.g. 0.0.0.0:9443")
    parser.add_argument("--orport", default=None, help="bridge Tor ORPort, e.g. 127.0.0.1:9001")
    parser.add_argument("--no-announce", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    cfg.ensure_dirs()
    log = setup_logging(
        "pt-server", cfg.path("paths.run") / "logs", level=10 if args.debug else 20
    )
    env = ServerEnv.from_environ(os.environ)
    bind = split_hostport(args.bind) if args.bind else env.bind_addrs.get(METHOD)
    orport = split_hostport(args.orport) if args.orport else env.orport
    ensure(CertPaths.from_config(cfg))
    server = ServerPT(cfg, orport=orport, bind=bind, log=log)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def runner() -> None:
        task = asyncio.create_task(server.serve(announce=not args.no_announce))
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
        await server.shutdown()

    try:
        loop.run_until_complete(runner())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        loop.close()
    return 0


def _wait_for_stdin_close(callback: Any) -> None:
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except Exception:
        pass
    with contextlib.suppress(Exception):
        callback()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
