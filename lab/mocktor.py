"""Tor stand-in for lab mode.

It does the two things Tor does for a managed client transport, and nothing else:

1. launches ``python -m pt.client`` with the ``TOR_PT_*`` environment and reads
   the ``CMETHOD`` line from stdout (FR-01);
2. accepts the browser's SOCKS5 connections, opens the PT's SOCKS channel with
   the bridge-line arguments (``persona=<id>``) in the username field, and relays
   the bytes through the tunnel (FR-07, D3).

Tor's circuits, encryption layers and relay network are not simulated; see
``docs/LAB_MODE.md`` for exactly what lab mode does and does not prove.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from config import Config
from lab.orport import encode_stream_header
from pt import socks5

PT_CLIENT_MODULE = "pt.client"


@dataclass(frozen=True, slots=True)
class PTMethod:
    """Parsed ``CMETHOD`` announcement."""

    name: str
    kind: str
    host: str
    port: int


def parse_cmethod(line: str) -> PTMethod | None:
    parts = line.split()
    if len(parts) < 4 or parts[0] != "CMETHOD":
        return None
    name, kind, address = parts[1], parts[2], parts[3]
    host, _, port = address.rpartition(":")
    try:
        return PTMethod(name=name.rstrip("-"), kind=kind, host=host.strip("[]"), port=int(port))
    except ValueError:
        return None


class SOCKSClient:
    """Minimal SOCKS5 client, used the way Tor uses the PT's listener."""

    @staticmethod
    async def connect(
        host: str, port: int, args: str = "", limit_s: float = 30.0  # noqa: ASYNC109
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), limit_s)
        try:
            if args:
                writer.write(b"\x05\x01\x02")
            else:
                writer.write(b"\x05\x01\x00")
            await writer.drain()
            choice = await reader.readexactly(2)
            if choice[0] != 5:
                raise socks5.SocksError(f"bad SOCKS version {choice[0]}")
            if choice[1] == socks5.METHOD_USERPASS:
                user = args.encode()
                writer.write(bytes([1, len(user)]) + user + b"\x00")
                await writer.drain()
                status = await reader.readexactly(2)
                if status != b"\x01\x00":
                    raise socks5.SocksError("PT rejected the client arguments")
            elif choice[1] != socks5.METHOD_NO_AUTH:
                raise socks5.SocksError("PT offered no usable auth method")
            writer.write(b"\x05\x01\x00\x01" + bytes([127, 0, 0, 1]) + (9).to_bytes(2, "big"))
            await writer.drain()
            head = await reader.readexactly(4)
            if head[0] != 5:
                raise socks5.SocksError("bad SOCKS reply")
            if head[1] != socks5.REP_SUCCESS:
                raise socks5.SocksError(f"PT refused the stream (code {head[1]})")
            if head[3] == socks5.ATYP_IPV4:
                await reader.readexactly(6)
            elif head[3] == socks5.ATYP_IPV6:
                await reader.readexactly(18)
            else:
                length = (await reader.readexactly(1))[0]
                await reader.readexactly(length + 2)
            return reader, writer
        except Exception:
            with contextlib.suppress(Exception):
                writer.close()
            raise


class MockTor:
    """A SOCKS5 front end backed by the real UQAB PT client."""

    def __init__(
        self,
        cfg: Config,
        *,
        persona: str,
        socks_host: str = "127.0.0.1",
        socks_port: int = 9150,
        bridge_host: str = "127.0.0.1",
        bridge_pt_port: int | None = None,
        log: logging.Logger | None = None,
        python: str | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        if config_path is None:
            from config import write_effective_config

            config_path = write_effective_config(
                cfg, cfg.path("paths.run") / "effective-config.yaml"
            )
        self.config_path = Path(config_path)
        self.persona = persona
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.bridge_host = bridge_host
        self.bridge_port = bridge_pt_port or cfg.get_int("bridge.pt_port", 9443)
        self.log = log or logging.getLogger("uqab.lab.mocktor")
        self.python = python or sys.executable
        self.process: asyncio.subprocess.Process | None = None
        self.method: PTMethod | None = None
        self.server: asyncio.Server | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._running = False
        self.connections = 0

    # ------------------------------------------------------------- PT client
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "TOR_PT_MANAGED_TRANSPORT_VER": "1",
                "TOR_PT_CLIENT_TRANSPORTS": "uqab",
                "TOR_PT_STATE_LOCATION": str(self.cfg.path("paths.run") / "pt-state"),
                "TOR_PT_EXIT_ON_STDIN_CLOSE": "1",
            }
        )
        (self.cfg.path("paths.run") / "pt-state").mkdir(parents=True, exist_ok=True)
        return env

    async def launch_pt_client(self, ready_timeout: float = 30.0) -> PTMethod:
        args = [
            self.python,
            "-u",
            "-m",
            PT_CLIENT_MODULE,
            "--config",
            str(self.config_path),
            "--bridge-host",
            self.bridge_host,
            "--bridge-port",
            str(self.bridge_port),
        ]
        self.log.info("launching managed PT client: %s", " ".join(args[2:]))
        self.process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
            cwd=str(self.cfg.root),
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        assert self.process.stdout is not None
        deadline = asyncio.get_running_loop().time() + ready_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("PT client did not announce CMETHOD in time")
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=remaining)
            if not line:
                raise RuntimeError("PT client exited before CMETHODS DONE")
            text = line.decode("utf-8", "replace").strip()
            self.log.debug("pt-client: %s", text)
            if text.startswith("VERSION"):
                continue
            if text.startswith("CMETHOD "):
                self.method = parse_cmethod(text)
                continue
            if text == "CMETHODS DONE":
                break
        if self.method is None:
            raise RuntimeError("PT client announced no usable CMETHOD")
        self.log.info(
            "PT client SOCKS method ready at %s:%d", self.method.host, self.method.port
        )
        asyncio.create_task(self._drain_stdout())
        return self.method

    async def _drain_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while True:
            line = await self.process.stdout.readline()
            if not line:
                return
            self.log.debug("pt-client: %s", line.decode("utf-8", "replace").rstrip())

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return
            self.log.info("pt-client: %s", line.decode("utf-8", "replace").rstrip())

    # ------------------------------------------------------------ SOCKS front
    async def start(self) -> int:
        await self.launch_pt_client()
        self.server = await asyncio.start_server(
            self._handle_browser, self.socks_host, self.socks_port
        )
        assert self.server.sockets
        self.socks_port = int(self.server.sockets[0].getsockname()[1])
        self._running = True
        self.log.info(
            "mock Tor SOCKS5 on %s:%d (persona %s)",
            self.socks_host,
            self.socks_port,
            self.persona,
        )
        return self.socks_port

    async def close(self) -> None:
        self._running = False
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None
        if self.process is not None and self.process.returncode is None:
            self.log.info("stopping PT client")
            if self.process.stdin is not None:
                with contextlib.suppress(Exception):
                    self.process.stdin.close()  # Tor closes stdin; the PT exits
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                with contextlib.suppress(Exception):
                    self.process.kill()
                    await self.process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task

    async def _handle_browser(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not self._running:
            writer.close()
            return
        self.connections += 1
        pt_reader: asyncio.StreamReader | None = None
        pt_writer: asyncio.StreamWriter | None = None
        try:
            await socks5.negotiate_auth(reader, writer)
            request = await socks5.read_request(reader)
            if request.command != socks5.CMD_CONNECT:
                await socks5.send_reply(writer, socks5.REP_CMD_NOT_SUPPORTED)
                return
            assert self.method is not None
            args_text = f"persona={self.persona}"
            pt_reader, pt_writer = await SOCKSClient.connect(
                self.method.host, self.method.port, args_text
            )
            pt_writer.write(encode_stream_header(request.host, request.port))
            await pt_writer.drain()
            await socks5.send_reply(writer, socks5.REP_SUCCESS)
            await asyncio.gather(
                self._pipe(reader, pt_writer),
                self._pipe(pt_reader, writer),
                return_exceptions=True,
            )
        except Exception as exc:
            self.log.debug("browser stream ended: %s", exc)
            with contextlib.suppress(Exception):
                await socks5.send_reply(writer, socks5.REP_GENERAL_FAILURE)
        finally:
            for stream in (pt_writer, writer):
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        total = 0
        try:
            while True:
                data = await reader.read(65_536)
                if not data:
                    break
                total += len(data)
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            logging.getLogger("uqab.lab.mocktor").debug("pipe finished after %d bytes", total)
            with contextlib.suppress(Exception):
                if writer.can_write_eof():
                    writer.write_eof()
