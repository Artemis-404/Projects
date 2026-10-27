"""One-command lab stack: demo origin + mock ORPort + PT server + mock Tor.

Run it with::

    python -m lab.stack --persona balanced --socks-port 9150

When it prints ``lab ready`` the browser (or the UQAB app) can use
``socks5://127.0.0.1:9150`` exactly as it would use Tor.  Everything after the
PT client is the production code path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any

from config import Config, load_config, write_effective_config
from lab.mocktor import MockTor
from lab.origin import DemoOrigin
from lab.orport import MockORPort
from logs import setup_logging
from pt.server import ServerPT
from tunnel.certs import CertPaths, ensure

READY_FILE = "lab.json"


def cfg_root() -> Path:
    return Path(__file__).resolve().parent.parent


def write_ready(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_ready(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class LabStack:
    """Owns every process and server needed for a Tor-free lab session."""

    def __init__(
        self,
        cfg: Config,
        *,
        persona: str = "balanced",
        socks_port: int = 9150,
        bridge_pt_port: int | None = None,
        origin_port: int | None = None,
        log: logging.Logger | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        # The PT client must read exactly the configuration this stack uses,
        # otherwise a redirected run dir or test certificate directory would be
        # applied on one side only.
        self.config_path = (
            Path(config_path)
            if config_path
            else write_effective_config(cfg, cfg.path("paths.run") / "effective-config.yaml")
        )
        self.persona = persona
        self.socks_port = socks_port
        self.bridge_pt_port = (
            cfg.get_int("bridge.pt_port", 9443) if bridge_pt_port is None else bridge_pt_port
        )
        self.origin_port = (
            cfg.get_int("lab.demo_origin_port", 8899) if origin_port is None else origin_port
        )
        self.log = log or logging.getLogger("uqab.lab.stack")
        self.origin = DemoOrigin("127.0.0.1", self.origin_port)
        self.orport: MockORPort | None = None
        self.server: ServerPT | None = None
        self.tor: MockTor | None = None
        self.ready_path = cfg.path("paths.run") / READY_FILE
        self._server_task: asyncio.Task[None] | None = None

    async def start(self) -> dict[str, Any]:
        ensure(CertPaths.from_config(self.cfg))
        actual_origin = self.origin.start()

        self.orport = MockORPort(actual_origin, log=self.log)
        orport_port = await self.orport.start()

        self.server = ServerPT(
            self.cfg,
            orport=("127.0.0.1", orport_port),
            bind=("127.0.0.1", self.bridge_pt_port),
            log=self.log,
        )
        self._server_task = asyncio.create_task(self.server.serve(announce=False))
        deadline = asyncio.get_running_loop().time() + 15
        while self.server.server is None:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("lab PT server did not start")
            await asyncio.sleep(0.02)
        assert self.server.server is not None and self.server.server.sockets
        self.bridge_pt_port = int(self.server.server.sockets[0].getsockname()[1])

        self.tor = MockTor(
            self.cfg,
            persona=self.persona,
            socks_port=self.socks_port,
            bridge_pt_port=self.bridge_pt_port,
            log=self.log,
            config_path=self.config_path,
        )
        self.socks_port = await self.tor.start()

        payload = {
            "ready": True,
            "mode": "simulated",
            "persona": self.persona,
            "socks_port": self.socks_port,
            "socks_host": "127.0.0.1",
            "bridge_pt_port": self.bridge_pt_port,
            "orport_port": orport_port,
            "origin_port": actual_origin,
            "pid": os.getpid(),
        }
        write_ready(self.ready_path, payload)
        self.log.info(
            "lab ready: socks 127.0.0.1:%d, bridge PT %d, origin %d",
            self.socks_port,
            self.bridge_pt_port,
            actual_origin,
        )
        return payload

    async def stop(self) -> None:
        self.log.info("stopping lab stack")
        if self.tor is not None:
            await self.tor.close()
        if self._server_task is not None:
            self._server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._server_task
        if self.server is not None:
            await self.server.shutdown()
        if self.orport is not None:
            await self.orport.close()
        with contextlib.suppress(Exception):
            self.origin.stop()
        with contextlib.suppress(FileNotFoundError):
            self.ready_path.unlink()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="UQAB lab simulation stack (no Tor)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--persona", default=None)
    parser.add_argument("--socks-port", type=int, default=9150)
    parser.add_argument("--pt-port", type=int, default=None)
    parser.add_argument("--origin-port", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    cfg_path = Path(args.config) if args.config else (cfg_root() / "config" / "config.yaml")
    cfg = load_config(cfg_path)
    persona = args.persona or cfg.get_str("persona.default", "balanced")
    log = setup_logging(
        "lab", cfg.path("paths.run") / "logs", level=10 if args.debug else 20
    )
    stack = LabStack(
        cfg,
        persona=persona,
        socks_port=args.socks_port,
        bridge_pt_port=args.pt_port,
        origin_port=args.origin_port,
        log=log,
        config_path=cfg_path,
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def runner() -> None:
        await stack.start()
        print(f"lab ready: socks5 127.0.0.1:{stack.socks_port}", flush=True)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await stack.stop()

    try:
        loop.run_until_complete(runner())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        loop.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
