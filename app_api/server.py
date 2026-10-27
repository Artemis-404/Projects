"""Local control API (FR-27 to FR-30, SDD 5.7).

HTTP on ``127.0.0.1:<app_api.port>`` only, and every request must carry the
app's ``Origin`` header; anything else is refused, so a web page the user happens
to visit cannot start or stop the tunnel.

The API is deliberately thin: it validates input, forwards start/stop to the
engine running on the asyncio loop, and reads the PT stats file for counters.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app_api.consent import CONSENT_TEXT, CONSENT_TEXT_VERSION, ConsentStore
from app_api.engine import (
    ENGINE_VERSION,
    Engine,
    EngineBusy,
    UnknownPersona,
)
from app_api.tor_control import describe_tor
from config import Config, ConfigError, load_config
from logs import setup_logging
from tunnel.certs import CertPaths, ensure

MAX_BODY_BYTES = 16_384
API_STATE_FILE = "api.json"


class EngineRunner:
    """Runs the asyncio engine on a dedicated thread for the HTTP handlers."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.loop = asyncio.new_event_loop()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(self._ready.set)
            self.loop.run_forever()

        self._thread = threading.Thread(target=run, name="uqab-engine", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def call(self, coro_factory: Any, limit_s: float = 180.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro_factory(), self.loop)
        return future.result(timeout=limit_s)

    def shutdown(self) -> None:
        if self._thread is None:
            return
        with contextlib.suppress(Exception):
            self.call(self.engine.shutdown, limit_s=15)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)
        with contextlib.suppress(Exception):
            self.loop.close()
        self._thread = None


class ControlAPI:
    """Request handling independent of the socket layer, so it is easy to test."""

    def __init__(
        self,
        engine: Engine,
        runner: EngineRunner,
        cfg: Config,
        log: logging.Logger | None = None,
    ) -> None:
        self.engine = engine
        self.runner = runner
        self.cfg = cfg
        self.log = log or logging.getLogger("uqab.api")
        self.allowed_origin = cfg.get_str("app_api.allowed_origin", "https://app.uqab")
        self.consent = ConsentStore.from_config(cfg)

    # ----------------------------------------------------------- utilities
    def origin_allowed(self, origin: str | None) -> bool:
        if not origin:
            return False
        return origin.rstrip("/") == self.allowed_origin.rstrip("/")

    def handle(
        self, method: str, path: str, payload: dict[str, Any] | None = None, origin: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        if not self.origin_allowed(origin):
            return HTTPStatus.FORBIDDEN, {
                "error": "origin_not_allowed",
                "detail": "requests must come from the UQAB app",
            }
        route = urlsplit(path).path.rstrip("/") or "/"
        try:
            return self._route(method.upper(), route, payload or {})
        except EngineBusy as exc:
            return HTTPStatus.CONFLICT, {"error": "busy", "detail": str(exc)}
        except UnknownPersona as exc:
            return HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "invalid_persona", "detail": str(exc)}
        except ValueError as exc:
            return HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(exc)}
        except TimeoutError:
            return HTTPStatus.GATEWAY_TIMEOUT, {"error": "timeout", "detail": "engine did not answer"}
        except Exception as exc:  # pragma: no cover - defensive
            self.log.exception("control API failure")
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal", "detail": str(exc)}

    def _route(self, method: str, route: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if route == "/health" and method == "GET":
            return HTTPStatus.OK, {
                "ok": True,
                "version": ENGINE_VERSION,
                "pid": os.getpid(),
                "uptime_s": round(time.time() - START_TIME, 1),
            }
        if route == "/status" and method == "GET":
            return HTTPStatus.OK, self.engine.status_dict()
        if route == "/start" and method == "POST":
            persona = payload.get("persona")
            if not isinstance(persona, str) or not persona:
                raise ValueError("body must be {\"persona\": \"<id>\"}")
            self.runner.call(lambda: self.engine.start(persona))
            return HTTPStatus.ACCEPTED, self.engine.status_dict()
        if route == "/stop" and method == "POST":
            self.runner.call(self.engine.stop)
            return HTTPStatus.ACCEPTED, self.engine.status_dict()
        if route == "/personas" and method == "GET":
            return HTTPStatus.OK, {"personas": self.engine.persona_ids()}
        if route == "/counters" and method == "GET":
            return HTTPStatus.OK, self.engine.counters_dict()
        if route == "/consent":
            if method == "GET":
                state = self.consent.read()
                return HTTPStatus.OK, {
                    **state.to_dict(),
                    "text": CONSENT_TEXT,
                    "current_text_version": CONSENT_TEXT_VERSION,
                    "profile_present": self.consent.profile_exists(),
                }
            if method == "POST":
                if payload.get("accept") is not True:
                    raise ValueError("consent requires {\"accept\": true}")
                version = str(payload.get("text_version", CONSENT_TEXT_VERSION))
                state = self.consent.accept(version)
                return HTTPStatus.OK, state.to_dict()
            if method == "DELETE":
                state = self.consent.withdraw()
                return HTTPStatus.OK, {**state.to_dict(), "profile_deleted": True}
        if route == "/mode" and method == "GET":
            return HTTPStatus.OK, {"mode": self.cfg.get_str("tor.mode", "auto")}
        if route == "/tor" and method == "GET":
            # The setup screen asks this to decide what to offer a new user.
            return HTTPStatus.OK, describe_tor(self.cfg)
        if route == "/setup" and method == "GET":
            return HTTPStatus.OK, {
                "tor": describe_tor(self.cfg),
                "mode": self.cfg.get_str("tor.mode", "auto"),
                "socks_port": self.cfg.get_int("tor.socks_port", 9150),
                "bridge": {
                    "host": self.cfg.get_str("bridge.host", "127.0.0.1"),
                    "port": self.cfg.get_int("bridge.pt_port", 9443),
                },
                "persona_default": self.cfg.get_str("persona.default", "balanced"),
                "llm": {
                    "url": self.cfg.get_str("llm.url", ""),
                    "model": self.cfg.get_str("llm.model", ""),
                },
                "config_path": str(getattr(self.cfg, "source", "") or ""),
            }
        return HTTPStatus.NOT_FOUND, {"error": "not_found", "detail": f"no route for {method} {route}"}


START_TIME = time.time()


class _Handler(BaseHTTPRequestHandler):
    server_version = f"UQABControlAPI/{ENGINE_VERSION}"
    api: ControlAPI

    # -------------------------------------------------------------- plumbing
    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("bad Content-Length") from exc
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("body must be JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("body must be a JSON object")
        return parsed

    def _dispatch(self, method: str) -> None:
        try:
            payload = self._read_body() if method in {"POST", "PUT", "DELETE"} else {}
        except ValueError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(exc)})
            return
        status, body = self.api.handle(
            method, self.path, payload, origin=self.headers.get("Origin")
        )
        self._send(status, body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(HTTPStatus.NO_CONTENT, {})

    def log_message(self, fmt: str, *args: object) -> None:
        logging.getLogger("uqab.api").debug("%s - %s", self.address_string(), fmt % args)


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], api: ControlAPI) -> None:
        handler = type("BoundHandler", (_Handler,), {"api": api})
        super().__init__(address, handler)
        self.api_impl = api


def create_api(cfg: Config, log: logging.Logger | None = None) -> tuple[Engine, EngineRunner, ControlAPI]:
    engine = Engine(cfg, log=log)
    runner = EngineRunner(engine)
    api = ControlAPI(engine, runner, cfg, log=log)
    return engine, runner, api


def serve(
    cfg: Config | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    log: logging.Logger | None = None,
) -> int:
    cfg = cfg or load_config()
    cfg.ensure_dirs()
    log = log or setup_logging("api", cfg.path("paths.run") / "logs")
    ensure(CertPaths.from_config(cfg))

    engine, runner, api = create_api(cfg, log)
    runner.start()
    bind_host = host or cfg.get_str("app_api.host", "127.0.0.1")
    bind_port = port if port is not None else cfg.get_int("app_api.port", 9170)
    try:
        server = ApiServer((bind_host, bind_port), api)
    except OSError as exc:
        log.error("control API cannot bind %s:%s: %s", bind_host, bind_port, exc)
        runner.shutdown()
        return 2
    actual_port = int(server.server_address[1])
    (cfg.path("paths.run") / API_STATE_FILE).write_text(
        json.dumps({"host": bind_host, "port": actual_port, "pid": os.getpid()}, indent=2),
        encoding="utf-8",
    )
    print(f"control API on http://{bind_host}:{actual_port}", flush=True)
    log.info("control API listening on %s:%s", bind_host, actual_port)

    stop = threading.Event()

    def request_stop(*_: Any) -> None:
        stop.set()

    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    thread = threading.Thread(target=server.serve_forever, name="uqab-api", daemon=True)
    thread.start()
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        log.info("shutting down control API")
        server.shutdown()
        server.server_close()
        runner.shutdown()
        with contextlib.suppress(FileNotFoundError):
            (cfg.path("paths.run") / API_STATE_FILE).unlink()
    return 0


def parse_assignments(pairs: list[str]) -> dict[str, object]:
    """Parse ``key=value`` overrides; values are read as JSON when possible.

    ``tor.bridges=["h:1 fp"]`` becomes a list, ``tor.socks_port=9150`` becomes an
    int, ``tor.binary=C:/tor/tor.exe`` stays a string. This is the only channel the
    GUI needs to change engine settings, so it accepts exactly what the settings
    screen can produce.
    """
    overrides: dict[str, object] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"override {pair!r} is not key=value")
        try:
            overrides[key] = json.loads(raw)
        except ValueError:
            overrides[key] = raw
    return overrides


def load_settings_file(path: Path) -> dict[str, object]:
    """Read a JSON object of dotted overrides written by the desktop app."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("settings file must contain a JSON object")
    return {str(key): value for key, value in payload.items()}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="UQAB local control API")
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--settings",
        default=None,
        help="JSON file of dotted overrides written by the desktop app",
    )
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="dotted configuration override, repeatable",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    overrides: dict[str, object] = {}
    if args.settings:
        try:
            overrides.update(load_settings_file(Path(args.settings)))
        except (OSError, ValueError) as exc:
            print(f"uqab: cannot read settings file: {exc}", file=sys.stderr)
            return 2
    try:
        overrides.update(parse_assignments(args.override))
    except ValueError as exc:
        print(f"uqab: {exc}", file=sys.stderr)
        return 2
    if overrides:
        cfg = cfg.with_overrides(**overrides)
    log = setup_logging(
        "api", cfg.path("paths.run") / "logs", level=10 if args.debug else 20
    )
    try:
        return serve(cfg, host=args.host, port=args.port, log=log)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
