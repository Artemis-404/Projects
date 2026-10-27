"""Run a UQAB bridge on this machine: a real Tor bridge relay with the UQAB PT server.

Real-Tor mode (`tor.mode: external`) needs a bridge that speaks UQAB. This script
starts one locally so the whole path can be exercised end to end:

    browser -> client Tor -> PT client -> shaped TLS tunnel -> PT server
            -> bridge Tor (this process) -> real Tor network -> destination

The bridge is not published (``PublishServerDescriptor 0``); only a client that
is told its address and fingerprint can use it. Because both ends run on one
machine, the shaped link itself is loopback: Tor circuits, relays and exits are
real, but an observer of *this* link sees no Internet path (see LAB_MODE.md).

Usage::

    python scripts/local_bridge.py --tor C:/path/to/tor.exe
    # prints the Bridge line and fingerprint, then runs until Ctrl+C
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_api.tor_control import torrc_quote  # noqa: E402
from config import load_config  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def write_bridge_torrc(
    run_dir: Path, *, python_exe: str, config_path: str, orport: int, pt_port: int
) -> Path:
    """The torrc for a private UQAB bridge on loopback."""
    data = run_dir / "data"
    data.mkdir(parents=True, exist_ok=True)
    lines = [
        "## UQAB local bridge - written by scripts/local_bridge.py",
        f"DataDirectory {torrc_quote(data)}",
        f"Log notice file {torrc_quote(run_dir / 'tor.log')}",
        "SocksPort 0",
        f"ORPort 127.0.0.1:{orport}",
        "BridgeRelay 1",
        "PublishServerDescriptor 0",
        "AssumeReachable 1",
        "ExitRelay 0",
        "Nickname uqablocal",
        f"ServerTransportPlugin uqab exec {torrc_quote(python_exe)} -u -m pt.server "
        f"--config {torrc_quote(config_path)}",
        f"ServerTransportListenAddr uqab 127.0.0.1:{pt_port}",
    ]
    torrc = run_dir / "torrc"
    torrc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return torrc


def read_fingerprint(run_dir: Path, wait_s: float = 60.0) -> str:
    """Tor writes ``<nickname> <FINGERPRINT>`` once its keys exist."""
    path = run_dir / "data" / "fingerprint"
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if path.is_file():
            parts = path.read_text(encoding="utf-8").split()
            if len(parts) >= 2:
                return parts[1]
        time.sleep(0.5)
    raise SystemExit(f"tor did not write {path} within {wait_s:.0f} s; see {run_dir / 'tor.log'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a private UQAB bridge on loopback")
    parser.add_argument("--tor", required=True, help="path to tor(.exe)")
    parser.add_argument("--config", default=str(REPO / "config" / "config.yaml"))
    parser.add_argument("--run-dir", default=None, help="default: <paths.run>/bridge")
    parser.add_argument("--orport", type=int, default=None, help="default: bridge.orport_port")
    parser.add_argument("--pt-port", type=int, default=None, help="default: bridge.pt_port")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    run_dir = Path(args.run_dir) if args.run_dir else cfg.path("paths.run") / "bridge"
    run_dir.mkdir(parents=True, exist_ok=True)
    orport = args.orport or cfg.get_int("bridge.orport_port", 9001)
    pt_port = args.pt_port or cfg.get_int("bridge.pt_port", 9443)
    torrc = write_bridge_torrc(
        run_dir,
        python_exe=sys.executable,
        config_path=args.config,
        orport=orport,
        pt_port=pt_port,
    )
    process = subprocess.Popen([args.tor, "-f", str(torrc)], cwd=str(REPO))
    try:
        fingerprint = read_fingerprint(run_dir)
        print(f"bridge fingerprint: {fingerprint}", flush=True)
        print(f"Bridge line for Setup: 127.0.0.1:{pt_port} {fingerprint}", flush=True)
        print(f"log: {run_dir / 'tor.log'}  (Ctrl+C to stop)", flush=True)
        return process.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
