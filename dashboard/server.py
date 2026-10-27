"""The dashboard HTTP server (FR-25, SDD 7).

Routes::

    /                       overview page (1 Hz refresh while a session is live)
    /runs                   every recorded run
    /run?id=<run_id>        one run in detail
    /api/model              the whole view model as JSON
    /api/comparison         the A/B/C table as JSON
    /api/stats              the live stats file as JSON
    /api/health             liveness probe
    /style.css              the shared stylesheet

The server is read-only: it opens no sockets except the listening one, and it is
bound to loopback by default.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from config import Config, load_config
from dashboard.data import load_model, run_detail
from dashboard.render import CSS, error_page, overview, page, run_page


class DashboardHandler(BaseHTTPRequestHandler):
    """Request handler bound to one configuration."""

    server_version = "UQAB-Dashboard/1.0"
    cfg: Config
    started_at = time.time()

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        parts = urlsplit(self.path)
        route = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        try:
            if route == "/":
                model = load_model(self.cfg)
                refresh = 5 if model.running else None
                self._html(overview(model, refresh_s=refresh))
            elif route == "/style.css":
                self._send(HTTPStatus.OK, CSS.encode("utf-8"), "text/css; charset=utf-8")
            elif route == "/runs":
                model = load_model(self.cfg)
                body = (
                    '<p class="muted"><a href="/">&larr; dashboard</a></p>'
                    "<h2>All runs</h2>"
                    + _runs_table(model.runs)
                )
                self._html(page("Runs", body))
            elif route == "/run":
                run_id = (query.get("id") or [""])[0]
                detail = run_detail(self.cfg, run_id) if run_id else None
                if detail is None:
                    self._html(error_page(f"no run with id {run_id!r}"), HTTPStatus.NOT_FOUND)
                else:
                    self._html(run_page(detail))
            elif route == "/api/model":
                self._json(load_model(self.cfg).to_dict())
            elif route == "/api/comparison":
                self._json({"configurations": load_model(self.cfg).comparison})
            elif route == "/api/stats":
                self._json(load_model(self.cfg).stats)
            elif route == "/api/health":
                self._json(
                    {
                        "ok": True,
                        "service": "dashboard",
                        "uptime_s": round(time.time() - self.started_at, 3),
                        "pid": os.getpid(),
                    }
                )
            else:
                self._html(
                    error_page(f"no route {parts.path!r}"), HTTPStatus.NOT_FOUND
                )
        except Exception as exc:  # noqa: BLE001 - the dashboard must not crash
            self._html(
                error_page(f"internal error: {type(exc).__name__}: {exc}", status=500),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------------------ output
    def _send(self, status: HTTPStatus, payload: bytes, content_type: str) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        sys.stderr.write(
            f"[dashboard] {self.address_string()} {fmt % args}\n"
        )


def _runs_table(runs: list[dict[str, Any]]) -> str:
    from dashboard.render import runs_table

    return runs_table(runs)


class DashboardServer(ThreadingHTTPServer):
    """Threaded HTTP server with a quiet flag used by the tests."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], cfg: Config, *, quiet: bool = False) -> None:
        self.quiet = quiet
        handler = type("BoundHandler", (DashboardHandler,), {"cfg": cfg})
        super().__init__(address, handler)


def serve(
    cfg: Config,
    *,
    host: str | None = None,
    port: int | None = None,
    quiet: bool = False,
) -> int:
    """Run the dashboard until interrupted; returns a process exit code."""
    address = host or cfg.get_str("dashboard.host", "127.0.0.1")
    chosen_port = int(port if port is not None else cfg.get_int("dashboard.port", 8501))
    server = DashboardServer((address, chosen_port), cfg, quiet=quiet)
    actual = server.server_address[1]
    info_path = cfg.path("paths.run") / "dashboard.json"
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(
        json.dumps({"host": address, "port": actual, "pid": os.getpid()}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"UQAB dashboard on http://{address}:{actual}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        pass
    finally:
        server.server_close()
        info_path.unlink(missing_ok=True)
    return 0


def start_background(
    cfg: Config, *, host: str = "127.0.0.1", port: int = 0, quiet: bool = True
) -> tuple[DashboardServer, threading.Thread]:
    """Start the dashboard on a thread (used by the app and by the tests)."""
    server = DashboardServer((host, port), cfg, quiet=quiet)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True, name="uqab-dashboard"
    )
    thread.start()
    return server, thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UQAB evaluation dashboard")
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config)) if args.config else load_config()
    return serve(cfg, host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
