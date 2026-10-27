"""Tor process control: torrc, cookie auth and bootstrap progress (FR-27, SDD 5.4).

The client engine owns Tor: it writes a torrc, starts ``tor``, authenticates on
the control port with the cookie Tor writes, reports bootstrap progress to the
app and shuts Tor down when the user presses Stop.

If no Tor binary is installed the engine falls back to the clearly-labelled lab
simulation (see ``lab/``), never to a direct connection (D9).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TOR_CONTROL_HOST = "127.0.0.1"

WINDOWS_TOR_CANDIDATES = (
    r"C:\Program Files\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
    r"C:\Program Files (x86)\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
    r"C:\Program Files\Tor\tor.exe",
    r"%LOCALAPPDATA%\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
    r"%USERPROFILE%\Desktop\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
)


class TorError(RuntimeError):
    """Tor could not be started or controlled."""


def find_tor_binary(cfg: object | None = None, extra_dirs: list[Path] | None = None) -> str | None:
    """Locate a Tor binary: config, PATH, the app bundle, then Windows defaults."""
    configured = ""
    if cfg is not None:
        with contextlib.suppress(Exception):
            configured = str(cfg.get_str("tor.binary", ""))  # type: ignore[attr-defined]
    if configured:
        candidate = Path(os.path.expandvars(configured))
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(configured)
        if found:
            return found
    found = shutil.which("tor")
    if found:
        return found
    search_dirs = list(extra_dirs or [])
    if getattr(sys, "frozen", False):
        search_dirs.append(Path(sys.executable).parent / "tor")
    for directory in search_dirs:
        for name in ("tor.exe", "tor"):
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    if os.name == "nt":
        for raw in WINDOWS_TOR_CANDIDATES:
            candidate = Path(os.path.expandvars(raw))
            if candidate.is_file():
                return str(candidate)
    return None


def tor_version(binary: str) -> str:
    """Run ``tor --version`` and return the first line, or an empty string."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (result.stdout or result.stderr or "").strip().splitlines()
    return first[0].strip() if first else ""


def describe_tor(cfg: object, extra_dirs: list[Path] | None = None) -> dict[str, object]:
    """Everything the setup screen needs to know about the local Tor.

    The app must never *require* the user to know where Tor is: it reports what it
    found, where, and which version, and the UI can then offer "use it" or "run the
    built-in lab simulation" as a single click.
    """
    configured = ""
    with contextlib.suppress(Exception):
        configured = str(cfg.get_str("tor.binary", ""))  # type: ignore[attr-defined]
    found = find_tor_binary(cfg, extra_dirs=extra_dirs)
    bundled = ""
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).parent / "tor" / ("tor.exe" if os.name == "nt" else "tor")
        if candidate.is_file():
            bundled = str(candidate)
    return {
        "found": bool(found),
        "path": found or "",
        "configured": configured,
        "configured_ok": bool(configured and Path(os.path.expandvars(configured)).is_file()),
        "version": tor_version(found) if found else "",
        "bundled": bool(found) and bool(bundled) and found == bundled,
        "platform": "windows" if os.name == "nt" else "posix",
        "search_dirs": [str(path) for path in (extra_dirs or [])],
        "hint": (
            "Tor found - UQAB can route through it."
            if found
            else "No Tor found: UQAB will run its built-in lab simulation until you "
            "point it at a tor binary in Setup."
        ),
    }


def write_torrc(
    cfg: object,
    run_dir: Path,
    persona: str,
    *,
    python_exe: str | None = None,
    config_path: str | None = None,
) -> Path:
    """Write the torrc that wires Tor to the UQAB client transport (SDD 5.4)."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    data_dir = run_dir / "tor"
    data_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)

    socks_port = cfg.get_int("tor.socks_port", 9150)  # type: ignore[attr-defined]
    control_port = cfg.get_int("tor.control_port", 9151)  # type: ignore[attr-defined]
    bridge_host = cfg.get_str("bridge.host", "127.0.0.1")  # type: ignore[attr-defined]
    bridge_pt_port = cfg.get_int("bridge.pt_port", 9443)  # type: ignore[attr-defined]
    fingerprint = cfg.get_str("bridge.fingerprint", "")  # type: ignore[attr-defined]
    python_exe = python_exe or sys.executable
    config_path = config_path or str(cfg.root / "config" / "config.yaml")  # type: ignore[attr-defined]

    plugin = (
        f'ClientTransportPlugin uqab exec "{python_exe}" -u -m pt.client '
        f'--config "{config_path}"'
    )
    bridge = f"Bridge uqab {bridge_host}:{bridge_pt_port}"
    if fingerprint.strip():
        bridge += f" {fingerprint.strip()}"
    bridge += f" persona={persona}"

    lines = [
        "## UQAB torrc - rewritten on every start (SDD 6.1)",
        f"SocksPort {TOR_CONTROL_HOST}:{socks_port}",
        f"ControlPort {TOR_CONTROL_HOST}:{control_port}",
        "CookieAuthentication 1",
        f'DataDirectory "{data_dir}"',
        f'Log notice file "{run_dir / "logs" / "tor.log"}"',
        "UseBridges 1",
        plugin,
        bridge,
        # Nothing may leave outside the tunnel: no direct fallback, no exit use.
        "ClientUseIPv6 0",
        "SafeLogging 1",
    ]
    torrc = run_dir / "torrc"
    torrc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return torrc


@dataclass(frozen=True, slots=True)
class BootstrapPhase:
    progress: int = 0
    tag: str = ""
    summary: str = ""
    severity: str = ""

    @classmethod
    def parse(cls, line: str) -> BootstrapPhase:
        # e.g. "NOTICE BOOTSTRAP PROGRESS=45 TAG=handshake_dir SUMMARY=\"Handshaking\""
        severity = ""
        tag = ""
        summary = ""
        progress = 0
        for token in line.split():
            if token.startswith("PROGRESS="):
                with contextlib.suppress(ValueError):
                    progress = int(token.split("=", 1)[1])
            elif token.startswith("TAG="):
                tag = token.split("=", 1)[1]
            elif token.startswith("SUMMARY="):
                summary = token.split("=", 1)[1].strip('"')
            elif token.isalpha() and not severity:
                severity = token
        return cls(progress=progress, tag=tag, summary=summary, severity=severity)


class TorController:
    """Minimal Tor control-port client: cookie auth, bootstrap, shutdown."""

    def __init__(
        self,
        host: str = TOR_CONTROL_HOST,
        port: int = 9151,
        cookie_path: Path | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.cookie_path = Path(cookie_path) if cookie_path else None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def connect(self, wait_s: float = 30.0, step_s: float = 0.25) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        last: Exception | None = None
        while loop.time() < deadline:
            try:
                self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
                return
            except OSError as exc:
                last = exc
                await asyncio.sleep(step_s)
        raise TorError(f"cannot reach Tor control port {self.host}:{self.port}: {last}")

    async def _read_reply(self, limit_s: float = 10.0) -> list[str]:
        assert self.reader is not None
        lines: list[str] = []
        while True:
            raw = await asyncio.wait_for(self.reader.readline(), timeout=limit_s)
            if not raw:
                raise TorError("Tor closed the control connection")
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("650"):
                continue  # asynchronous event
            lines.append(line)
            if len(line) >= 4 and line[3] == " ":
                return lines
            if line in {"250 OK", "251 OK"}:
                return lines

    async def command(self, command: str, limit_s: float = 10.0) -> list[str]:
        if self.writer is None:
            raise TorError("control connection is not open")
        self.writer.write((command + "\r\n").encode("ascii"))
        await self.writer.drain()
        return await self._read_reply(limit_s)

    async def authenticate(self) -> None:
        if self.cookie_path is None:
            raise TorError("no cookie path configured")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        cookie = b""
        while loop.time() < deadline:
            try:
                cookie = self.cookie_path.read_bytes()
                if cookie:
                    break
            except OSError:
                pass
            await asyncio.sleep(0.1)
        if not cookie:
            raise TorError(f"Tor cookie file not found at {self.cookie_path}")
        reply = await self.command(f"AUTHENTICATE {cookie.hex()}")
        if not any(line.startswith("250") for line in reply):
            raise TorError(f"authentication failed: {reply}")

    async def bootstrap(self) -> BootstrapPhase:
        reply = await self.command("GETINFO status/bootstrap-phase")
        for line in reply:
            if "status/bootstrap-phase=" in line:
                return BootstrapPhase.parse(line.split("=", 1)[1])
        return BootstrapPhase()

    async def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            await self.command("SIGNAL SHUTDOWN", limit_s=5)
        await self.close()

    async def close(self) -> None:
        if self.writer is not None:
            with contextlib.suppress(Exception):
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), timeout=2)
            self.writer = None
            self.reader = None
