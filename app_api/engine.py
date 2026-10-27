"""Client engine: start/stop Tor (or the lab simulation) and report status.

The engine is the only component that starts or stops the tunnel.  It owns the
short-lived processes (``tor`` or the lab stack), reads the PT client's stats
file, and exposes the state machine the app and dashboard consume:

``stopped -> connecting -> connected -> blocked -> ...``

It never shapes traffic and never talks to the browser directly; the app only
calls this engine through the local control API (D1, D10).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app_api.consent import ConsentStore
from app_api.tor_control import TorController, TorError, find_tor_binary, write_torrc
from config import Config, load_config, write_effective_config
from lab.stack import READY_FILE
from logs import setup_logging
from persona.validator import (
    Limits,
    PersonaValidationError,
    load_by_id,
    validated_personas,
)
from pt.stats import read_stats, stats_age_s, write_stats_atomic
from tunnel.certs import CertPaths, ensure

ENGINE_VERSION = "1.0.0"
ENGINE_STATE_FILE = "engine.json"


class EngineBusy(RuntimeError):
    """A session is already running (FR-27, FR-28)."""


class UnknownPersona(ValueError):
    """The requested persona is not a validated persona (FR-28)."""


@dataclass(slots=True)
class EngineStatus:
    state: str = "stopped"
    bootstrap: int = 0
    persona: str = ""
    since: float = 0.0
    mode: str = ""
    reason: str = ""
    detail: str = ""
    socks_port: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "bootstrap": int(self.bootstrap),
            "persona": self.persona,
            "since": self.since,
            "mode": self.mode,
            "socks_port": int(self.socks_port),
            "reason": self.reason,
            "detail": self.detail,
            "engine_version": ENGINE_VERSION,
        }


@dataclass(slots=True)
class Health:
    bootstrap: int = 0
    alive: bool = True
    detail: str = ""


@dataclass(slots=True)
class LabBackend:
    """Runs ``lab.stack``: demo origin, mock ORPort, PT server and Tor stand-in."""

    cfg: Config
    run_dir: Path
    persona: str
    log: logging.Logger
    config_path: Path | None = None
    socks_port: int = 9150
    proc: asyncio.subprocess.Process | None = None
    ready: dict[str, Any] | None = None
    _log_task: asyncio.Task[None] | None = None
    label: str = field(default="simulated", init=False)

    @property
    def ready_path(self) -> Path:
        return self.run_dir / READY_FILE

    async def start(self) -> None:
        await self._kill_stale()
        with contextlib.suppress(FileNotFoundError):
            self.ready_path.unlink()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "lab.stack",
            "--config",
            str(self.config_path or (self.cfg.root / "config" / "config.yaml")),
            "--persona",
            self.persona,
            "--socks-port",
            str(self.socks_port),
            "--pt-port",
            str(self.cfg.get_int("bridge.pt_port", 9443)),
            "--origin-port",
            str(self.cfg.get_int("lab.demo_origin_port", 8899)),
        ]
        self.log.info("starting lab stack: %s", " ".join(cmd[2:]))
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.cfg.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self._log_task = asyncio.create_task(self._forward_logs())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 90
        while loop.time() < deadline:
            if self.proc.returncode is not None:
                raise TorError(
                    "lab stack exited during start-up; see run/logs/lab.log"
                )
            info = self._read_ready()
            if info and info.get("ready"):
                self.ready = info
                # Adopt the port the stack actually bound (0 means "any free port"),
                # so the app and the browser route to the real listener.
                port = info.get("socks_port")
                if isinstance(port, int) and port > 0:
                    self.socks_port = port
                self.log.info("lab stack ready on socks port %s", self.socks_port)
                return
            await asyncio.sleep(0.1)
        raise TorError("lab stack did not become ready within 90 s")

    def _read_ready(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.ready_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def _forward_logs(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return
            self.log.debug("lab: %s", line.decode("utf-8", "replace").rstrip())

    async def _kill_stale(self) -> None:
        info = self._read_ready()
        if not info:
            return
        pid = info.get("pid")
        if isinstance(pid, int) and pid != os.getpid():
            self.log.warning("stopping stale lab stack (pid %s)", pid)
            with contextlib.suppress(Exception):
                os.kill(pid, 15)
            await asyncio.sleep(0.3)
        with contextlib.suppress(FileNotFoundError):
            self.ready_path.unlink()

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except TimeoutError:
                with contextlib.suppress(Exception):
                    self.proc.kill()
                    await self.proc.wait()
        if self._log_task is not None:
            self._log_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._log_task
            self._log_task = None
        self.proc = None
        self.ready = None
        with contextlib.suppress(FileNotFoundError):
            self.ready_path.unlink()

    async def poll(self) -> Health:
        alive = self.proc is not None and self.proc.returncode is None
        if self.ready is None:
            self.ready = self._read_ready()
        bootstrap = 100 if self.ready else (30 if alive else 0)
        detail = "lab simulation" if self.ready else "starting lab stack"
        return Health(bootstrap=bootstrap, alive=alive, detail=detail)


@dataclass(slots=True)
class TorBackend:
    """Runs a real Tor with the UQAB client transport plugin."""

    cfg: Config
    run_dir: Path
    persona: str
    log: logging.Logger
    config_path: Path | None = None
    binary: str = ""
    proc: asyncio.subprocess.Process | None = None
    controller: TorController | None = None
    _last: Health = field(default_factory=Health)
    label: str = field(default="tor", init=False)

    async def start(self) -> None:
        binary = self.binary or find_tor_binary(self.cfg)
        if not binary:
            raise TorError(
                "Tor is not installed; set tor.binary in config.yaml, install Tor, "
                "or use the lab simulation (tor.mode: simulated)"
            )
        torrc = write_torrc(
            self.cfg,
            self.run_dir,
            self.persona,
            config_path=str(self.config_path) if self.config_path else None,
        )
        self.log.info("starting Tor: %s -f %s", binary, torrc)
        self.proc = await asyncio.create_subprocess_exec(
            binary,
            "-f",
            str(torrc),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self.run_dir),
        )
        data_dir = self.run_dir / "tor"
        cookie = data_dir / "control_auth_cookie"
        self.controller = TorController(
            port=self.cfg.get_int("tor.control_port", 9151), cookie_path=cookie
        )
        await self.controller.connect(wait_s=45)
        await self.controller.authenticate()
        with contextlib.suppress(TorError):
            await self.controller.command("SETEVENTS STATUS_CLIENT")
        self._last = Health(bootstrap=0, alive=True, detail="Tor starting")

    async def stop(self) -> None:
        if self.controller is not None:
            await self.controller.shutdown()
            self.controller = None
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=10)
            except TimeoutError:
                with contextlib.suppress(Exception):
                    self.proc.kill()
                    await self.proc.wait()
        self.proc = None

    async def poll(self) -> Health:
        alive = self.proc is not None and self.proc.returncode is None
        if not alive:
            return Health(bootstrap=0, alive=False, detail="Tor exited")
        if self.controller is None:
            return Health(bootstrap=0, alive=True, detail="Tor starting")
        try:
            phase = await self.controller.bootstrap()
        except Exception as exc:
            self.log.debug("bootstrap poll failed: %s", exc)
            return Health(bootstrap=self._last.bootstrap, alive=True, detail="Tor control busy")
        self._last = Health(
            bootstrap=phase.progress,
            alive=True,
            detail=phase.summary or phase.tag or "bootstrapping",
        )
        return self._last


class Engine:
    """Owns the tunnel lifecycle and the status the app displays."""

    def __init__(self, cfg: Config | None = None, log: logging.Logger | None = None) -> None:
        self.cfg = cfg or load_config()
        self.log = log or setup_logging("engine")
        self.cfg.ensure_dirs()
        self.run_dir = self.cfg.path("paths.run")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.stats_path = self.run_dir / "stats.json"
        self.limits = Limits.from_config(self.cfg)
        self.profiles_dir = self.cfg.path("persona.profiles_dir", "persona/profiles")
        self.consent = ConsentStore.from_config(self.cfg)
        self.status = EngineStatus()
        self.config_path = write_effective_config(self.cfg, self.run_dir / "effective-config.yaml")
        self.backend: LabBackend | TorBackend | None = None
        self.monitor_task: asyncio.Task[None] | None = None
        self._lock = threading.RLock()

    # -------------------------------------------------------------- helpers
    def persona_ids(self) -> list[dict[str, Any]]:
        return [
            {
                "id": p.id,
                "source": p.source,
                "description": p.description,
            }
            for p in validated_personas(self.profiles_dir, self.limits)
        ]

    def _persona(self, persona_id: str) -> Any:
        try:
            return load_by_id(persona_id, self.profiles_dir, self.limits)
        except PersonaValidationError as exc:
            raise UnknownPersona(f"persona {persona_id!r} is not valid or not present") from exc

    def _resolve_mode(self) -> str:
        configured = self.cfg.get_str("tor.mode", "auto").strip().lower()
        if configured in {"external", "tor"}:
            return "tor"
        if configured in {"simulated", "lab"}:
            return "simulated"
        return "tor" if find_tor_binary(self.cfg) else "simulated"

    def _set_status(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self.status, key, value)

    def status_dict(self) -> dict[str, Any]:
        with self._lock:
            data = self.status.to_dict()
        data["counters"] = self.counters_dict()
        data["consent"] = self.consent.read().to_dict()
        data["mode_label"] = (
            "Tor" if data.get("mode") == "tor" else "Lab simulation" if data.get("mode") else ""
        )
        return data

    def counters_dict(self) -> dict[str, Any]:
        stats = read_stats(self.stats_path) or {}
        stale = stats_age_s(stats) > 5.0
        state = self.status.state
        if state in {"stopped"} or stale:
            return {
                "real_up": 0,
                "real_down": 0,
                "cover_up": 0,
                "cover_down": 0,
                "overhead": 0.0,
                "period": "",
                "t": time.time(),
                "stale": True,
            }
        return {
            "real_up": int(stats.get("real_up", 0)),
            "real_down": int(stats.get("real_down", 0)),
            "cover_up": int(stats.get("cover_up", 0)),
            "cover_down": int(stats.get("cover_down", 0)),
            "overhead": float(stats.get("overhead", 0.0)),
            "period": str(stats.get("period", "")) or "",
            "persona": str(stats.get("persona", "")) or self.status.persona,
            "t": float(stats.get("t", time.time())),
            "reason": str(stats.get("reason", "")),
            "stale": False,
        }

    # ------------------------------------------------------------ lifecycle
    async def start(self, persona_id: str) -> dict[str, Any]:
        with self._lock:
            if self.status.state not in {"stopped", "blocked"}:
                raise EngineBusy(f"engine is {self.status.state}")
        persona = self._persona(persona_id)
        ensure(CertPaths.from_config(self.cfg))
        mode = self._resolve_mode()
        socks_port = int(self.cfg.get_int("tor.socks_port", 9150))
        self._set_status(
            state="connecting",
            bootstrap=5,
            persona=persona.id,
            since=time.time(),
            mode=mode,
            socks_port=socks_port,
            reason="",
            detail="starting " + ("Tor" if mode == "tor" else "lab simulation"),
        )
        if mode == "tor":
            backend: LabBackend | TorBackend = TorBackend(
                self.cfg, self.run_dir, persona.id, self.log, config_path=self.config_path
            )
        else:
            backend = LabBackend(
                self.cfg,
                self.run_dir,
                persona.id,
                self.log,
                config_path=self.config_path,
                socks_port=self.cfg.get_int("tor.socks_port", 9150),
            )
        self.backend = backend
        try:
            await backend.start()
        except Exception as exc:
            self.log.error("failed to start %s: %s", mode, exc)
            with contextlib.suppress(Exception):
                await backend.stop()
            self.backend = None
            self._set_status(state="blocked", bootstrap=0, reason=str(exc), detail="start failed")
            self._write_engine_file()
            raise
        actual_port = getattr(backend, "socks_port", None)
        if isinstance(actual_port, int) and actual_port > 0:
            self._set_status(socks_port=actual_port)
        self._start_monitor()
        self._write_engine_file()
        return self.status_dict()

    async def stop(self, reason: str = "stopped by user") -> dict[str, Any]:
        # The port stays in the status so the UI can explain what was stopped;
        # the app simply stops routing once the state leaves {connecting, connected}.
        if self.monitor_task is not None:
            self.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.monitor_task
            self.monitor_task = None
        if self.backend is not None:
            with contextlib.suppress(Exception):
                await self.backend.stop()
            self.backend = None
        # Replace the PT stats file so a later reader cannot see a stale session.
        with contextlib.suppress(Exception):
            write_stats_atomic(
                self.stats_path,
                {
                    "t": time.time(),
                    "state": "stopped",
                    "persona": self.status.persona,
                    "period": "",
                    "real_up": 0,
                    "real_down": 0,
                    "cover_up": 0,
                    "cover_down": 0,
                    "overhead": 0.0,
                    "bootstrap": 0,
                    "reason": reason,
                },
            )
        self._set_status(state="stopped", bootstrap=0, reason=reason, detail="")
        self._write_engine_file()
        return self.status_dict()

    def _start_monitor(self) -> None:
        if self.monitor_task is None or self.monitor_task.done():
            self.monitor_task = asyncio.create_task(self._monitor(), name="uqab-engine-monitor")

    async def _monitor(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - the monitor must survive
                self.log.exception("engine monitor tick failed")
            await asyncio.sleep(0.5)

    async def _tick(self) -> None:
        backend = self.backend
        if backend is None:
            return
        health = await backend.poll()
        stats = read_stats(self.stats_path)
        pt_state = str(stats.get("state", "")) if stats else ""
        stale = stats_age_s(stats) > 5.0
        with self._lock:
            mode = self.status.mode
        if not health.alive:
            self._set_status(
                state="blocked",
                bootstrap=0,
                reason="the tunnel backend exited; no traffic can leave",
                detail=health.detail,
            )
        elif pt_state == "blocked" and not stale:
            # A tunnel failure always wins: the app must show Blocked (UC-6).
            self._set_status(
                state="blocked",
                bootstrap=min(99, max(5, health.bootstrap)),
                reason=str(stats.get("reason", "")) if stats else "",
                detail=health.detail,
            )
        elif health.bootstrap >= 100 or (pt_state == "connected" and not stale):
            # Tor finishing its bootstrap through the bridge *is* proof the PT
            # tunnel works, and in lab mode the stack reports 100 when the mock
            # bridge is listening. "Connected" therefore means "you may browse",
            # not "a page has been loaded": a session only exists after the first
            # request, which the browser cannot make before Connected.
            self._set_status(state="connected", bootstrap=100, reason="", detail=health.detail)
        else:
            self._set_status(
                state="connecting",
                bootstrap=max(5, min(99, health.bootstrap)),
                detail=health.detail,
            )
        self._set_status(mode=mode)
        self._write_engine_file()

    def _write_engine_file(self) -> None:
        url = self.run_dir / ENGINE_STATE_FILE
        with self._lock:
            payload = self.status.to_dict()
        payload["pid"] = os.getpid()
        with contextlib.suppress(Exception):
            tmp = url.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, url)

    async def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            await self.stop(reason="engine shutdown")


def read_engine_file(run_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((run_dir / ENGINE_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def engine_binary_dir() -> Path:
    """Directory of the running interpreter (used to find bundled tools)."""
    return Path(sys.executable).parent


def has_tor(cfg: Config | None = None) -> bool:
    return find_tor_binary(cfg) is not None


def subprocess_which(name: str) -> str | None:
    return shutil.which(name)
