"""Real-browser experiment driver: Chromium through the UQAB SOCKS port (FR-20).

``experiments.driver`` picks how the scripted timeline fetches pages:

* ``playwright`` - headless Chromium driven by Playwright. One navigation loads
  the document *and* its subresources the way a user's browser does, and the
  load time is the browser's own ``load`` event. Requires the optional extra:
  ``pip install -e ".[browser]"`` then ``python -m playwright install chromium``.
* ``http`` - the small SOCKS5 HTTP client in :mod:`experiments.socks_http`.
* ``auto`` (default) - Playwright when it is installed and Chromium starts,
  otherwise ``http``. The driver actually used is recorded with every run.

Chromium is pinned to the proxy: every hostname is resolved by the proxy
(``--host-resolver-rules`` makes local DNS fail), loopback is not bypassed, QUIC
is off and WebRTC may not send UDP outside the proxy, so the browser cannot
reach the network except through the tunnel under test.

Playwright's sync API is bound to the thread that started it, so a driver is
opened, used and closed inside the replay thread (:func:`replay_with_driver`).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from experiments.socks_http import HttpError, PageLoad, fetch_sync

DRIVERS = ("auto", "playwright", "http")

# Every flag closes a path that would bypass the SOCKS proxy.
CHROMIUM_ARGS = (
    "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--no-first-run",
    "--no-default-browser-check",
)


class DriverUnavailable(RuntimeError):
    """The requested driver cannot run on this machine."""


def proxy_settings(proxy_host: str, proxy_port: int) -> dict[str, str]:
    """Playwright proxy settings: SOCKS5 with remote DNS, nothing bypassed."""
    return {"server": f"socks5://{proxy_host}:{proxy_port}", "bypass": "<-loopback>"}


@dataclass(slots=True)
class PlaywrightDriver:
    """Headless Chromium that fetches each URL as a fresh page view."""

    proxy_host: str
    proxy_port: int
    headless: bool = True
    launcher: Callable[[], Any] | None = None  # injectable for tests
    _manager: Any = field(default=None, init=False, repr=False)
    _playwright: Any = field(default=None, init=False, repr=False)
    _browser: Any = field(default=None, init=False, repr=False)

    name = "playwright"

    def open(self) -> PlaywrightDriver:
        try:
            if self.launcher is not None:
                self._manager = self.launcher()
            else:
                from playwright.sync_api import sync_playwright

                self._manager = sync_playwright()
            self._playwright = self._manager.start()
            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                proxy=proxy_settings(self.proxy_host, self.proxy_port),
                args=list(CHROMIUM_ARGS),
            )
        except ImportError as exc:
            raise DriverUnavailable(
                "playwright is not installed: pip install -e \".[browser]\" "
                "and python -m playwright install chromium"
            ) from exc
        except Exception as exc:
            self.close()
            raise DriverUnavailable(f"Chromium could not start: {exc}") from exc
        return self

    def fetch(self, url: str, proxy_host: str, proxy_port: int, *, timeout: float) -> PageLoad:
        """Load ``url`` in a new browser context (empty cache, no cookies)."""
        del proxy_host, proxy_port  # the browser is already pinned to the proxy
        if self._browser is None:
            raise HttpError("the browser is not running")
        context = self._browser.new_context(ignore_https_errors=False)
        # Collect finished requests here and size them after the load: blocking
        # calls such as Request.sizes() are not allowed inside sync-API callbacks.
        finished: list[Any] = []
        try:
            page = context.new_page()
            page.on("requestfinished", lambda request: finished.append(request))
            started = time.perf_counter()
            try:
                response = page.goto(url, wait_until="load", timeout=timeout * 1000.0)
            except Exception as exc:  # noqa: BLE001 - navigation errors become HttpError
                raise HttpError(f"{type(exc).__name__}: {exc}".splitlines()[0]) from exc
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            status = int(response.status) if response is not None else 0
            received = sum(_response_bytes(request) for request in finished)
            return PageLoad(url=url, status=status, bytes_received=received, load_ms=elapsed_ms)
        finally:
            context.close()

    def close(self) -> None:
        for closer in (
            getattr(self._browser, "close", None),
            getattr(self._manager, "stop", None) if self._manager is not None else None,
        ):
            if closer is not None:
                with contextlib.suppress(Exception):  # closing is best effort
                    closer()
        self._browser = None
        self._playwright = None
        self._manager = None

    def __enter__(self) -> PlaywrightDriver:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _response_bytes(request: Any) -> int:
    """Response headers + body for one finished request (best effort)."""
    try:
        sizes = request.sizes()
        return int(sizes.get("responseBodySize", 0)) + int(sizes.get("responseHeadersSize", 0))
    except Exception:  # noqa: BLE001 - a request that cannot be sized counts as 0
        return 0


@dataclass(slots=True)
class HttpDriver:
    """The Playwright-less fallback: one SOCKS5 HTTP GET per fetch."""

    name = "http"

    def fetch(self, url: str, proxy_host: str, proxy_port: int, *, timeout: float) -> PageLoad:
        return fetch_sync(url, proxy_host, proxy_port, timeout=timeout)

    def close(self) -> None:
        return None


def open_driver(
    kind: str,
    proxy_host: str,
    proxy_port: int,
    *,
    launcher: Callable[[], Any] | None = None,
) -> tuple[PlaywrightDriver | HttpDriver, str]:
    """Open the requested driver; returns it with a note on what was chosen."""
    kind = (kind or "auto").strip().lower()
    if kind not in DRIVERS:
        raise DriverUnavailable(f"unknown experiments.driver {kind!r}; use one of {DRIVERS}")
    if kind == "http":
        return HttpDriver(), "driver: http (SOCKS5 HTTP client)"
    try:
        driver = PlaywrightDriver(proxy_host, proxy_port, launcher=launcher).open()
        return driver, "driver: playwright (headless Chromium)"
    except DriverUnavailable as exc:
        if kind == "playwright":
            raise
        return HttpDriver(), f"driver: http (Playwright unavailable: {exc})"


def replay_with_driver(
    replay_fn: Callable[..., Any],
    timeline: Any,
    *,
    driver: str,
    proxy_host: str,
    proxy_port: int,
    launcher: Callable[[], Any] | None = None,
    **kwargs: Any,
) -> tuple[Any, str]:
    """Open the driver, replay the timeline through it, close it - in one thread."""
    chosen, note = open_driver(driver, proxy_host, proxy_port, launcher=launcher)
    try:
        outcome = replay_fn(
            timeline,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            fetch_fn=chosen.fetch,
            **kwargs,
        )
    finally:
        chosen.close()
    return outcome, note
