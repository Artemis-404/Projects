"""Real-browser experiment driver (FR-20).

Unit tests use a stand-in for Playwright, so they run everywhere. The last test
drives real headless Chromium through the lab stack and is skipped when
Playwright or its Chromium is not installed (``pip install -e ".[browser]"``).
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any

import pytest

from config import Config
from experiments.browser import (
    CHROMIUM_ARGS,
    DriverUnavailable,
    HttpDriver,
    PlaywrightDriver,
    open_driver,
    proxy_settings,
    replay_with_driver,
)
from experiments.runner import Phase, Timeline, replay
from experiments.socks_http import HttpError
from tests.conftest import run


class FakeRequest:
    def __init__(self, body: int, headers: int) -> None:
        self._sizes = {"responseBodySize": body, "responseHeadersSize": headers}

    def sizes(self) -> dict[str, int]:
        return self._sizes


class BrokenRequest:
    def sizes(self) -> dict[str, int]:
        raise RuntimeError("request was aborted")


class FakeResponse:
    status = 200


class FakePage:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    def goto(self, url: str, *, wait_until: str, timeout: float) -> FakeResponse:
        assert wait_until == "load"
        assert timeout > 0
        if self.fail:
            raise RuntimeError("net::ERR_SOCKS_CONNECTION_FAILED at " + url)
        for body, headers in ((4000, 200), (900, 100)):  # document + one subresource
            self.handlers["requestfinished"](FakeRequest(body, headers))
        self.handlers["requestfinished"](BrokenRequest())  # unsizable: counts as 0
        return FakeResponse()


class FakeContext:
    def __init__(self, owner: FakeBrowser) -> None:
        self.owner = owner
        self.closed = False

    def new_page(self) -> FakePage:
        return FakePage(self.owner.fail)

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.contexts: list[FakeContext] = []
        self.closed = False

    def new_context(self, **_: Any) -> FakeContext:
        context = FakeContext(self)
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.launches: list[dict[str, Any]] = []
        self.browser: FakeBrowser | None = None

    def launch(self, **kwargs: Any) -> FakeBrowser:
        self.launches.append(kwargs)
        self.browser = FakeBrowser(self.fail)
        return self.browser


class FakeManager:
    def __init__(self, fail: bool = False) -> None:
        self.chromium = FakeChromium(fail)
        self.stopped = False

    def start(self) -> FakeManager:
        return self

    def stop(self) -> None:
        self.stopped = True


def test_chromium_is_pinned_to_the_socks_proxy() -> None:
    manager = FakeManager()
    driver = PlaywrightDriver("127.0.0.1", 9150, launcher=lambda: manager).open()
    launch = manager.chromium.launches[0]
    assert launch["proxy"] == {"server": "socks5://127.0.0.1:9150", "bypass": "<-loopback>"}
    assert launch["proxy"] == proxy_settings("127.0.0.1", 9150)
    args = launch["args"]
    # No local DNS, no QUIC, no non-proxied WebRTC: nothing can bypass the tunnel.
    assert any(arg.startswith("--host-resolver-rules=MAP * ~NOTFOUND") for arg in args)
    assert "--disable-quic" in args
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert tuple(args) == CHROMIUM_ARGS
    driver.close()
    assert manager.stopped


def test_fetch_counts_every_response_and_uses_a_fresh_context() -> None:
    manager = FakeManager()
    with PlaywrightDriver("127.0.0.1", 9150, launcher=lambda: manager) as driver:
        first = driver.fetch("http://demo.uqab/", "127.0.0.1", 9150, timeout=5)
        driver.fetch("http://demo.uqab/docs", "127.0.0.1", 9150, timeout=5)
        browser = manager.chromium.browser
        assert browser is not None
        assert len(browser.contexts) == 2 and all(c.closed for c in browser.contexts)
    assert first.status == 200
    assert first.bytes_received == 4000 + 200 + 900 + 100
    assert first.load_ms >= 0


def test_navigation_errors_become_http_errors() -> None:
    manager = FakeManager(fail=True)
    with PlaywrightDriver("127.0.0.1", 9150, launcher=lambda: manager) as driver:
        with pytest.raises(HttpError, match="ERR_SOCKS"):
            driver.fetch("http://example.com/", "127.0.0.1", 9150, timeout=5)
        browser = manager.chromium.browser
        assert browser is not None and browser.contexts[0].closed


def _missing() -> Any:
    raise ImportError("No module named 'playwright'")


def test_auto_falls_back_to_http_and_says_so() -> None:
    driver, note = open_driver("auto", "127.0.0.1", 9150, launcher=_missing)
    assert isinstance(driver, HttpDriver)
    assert note.startswith("driver: http (Playwright unavailable")


def test_explicit_playwright_does_not_fall_back() -> None:
    with pytest.raises(DriverUnavailable, match="not installed"):
        open_driver("playwright", "127.0.0.1", 9150, launcher=_missing)
    with pytest.raises(DriverUnavailable, match="unknown"):
        open_driver("selenium", "127.0.0.1", 9150)


def test_replay_runs_through_the_chosen_driver() -> None:
    manager = FakeManager()
    timeline = Timeline(
        phases=[
            Phase(kind="load", duration_s=0.2, url="http://demo.uqab/", site="demo.uqab", label="active"),
            Phase(kind="idle", duration_s=0.1, site="demo.uqab", label="idle"),
        ]
    )
    outcome, note = replay_with_driver(
        replay,
        timeline,
        driver="playwright",
        proxy_host="127.0.0.1",
        proxy_port=9150,
        launcher=lambda: manager,
        requests_per_load=1,
    )
    assert note == "driver: playwright (headless Chromium)"
    assert outcome.loads[0].ok and outcome.loads[0].bytes == 5200
    assert manager.stopped, "the browser is closed after the replay"


def _chromium_available() -> bool:
    if importlib.util.find_spec("playwright") is None:
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001 - any failure means "skip"
        return False


@pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")
def test_real_chromium_loads_lab_pages_only_through_the_tunnel(cfg: Config) -> None:
    from lab.stack import LabStack

    async def scenario() -> None:
        stack = LabStack(cfg, persona="high", socks_port=0, bridge_pt_port=0, origin_port=0)
        info = await stack.start()
        try:
            port = int(info["socks_port"])

            def browse() -> tuple[Any, str]:
                driver = PlaywrightDriver("127.0.0.1", port).open()
                try:
                    # demo.uqab does not resolve on this machine: loading it at
                    # all proves the browser used remote DNS through the proxy.
                    page = driver.fetch("http://demo.uqab/", "127.0.0.1", port, timeout=30)
                    try:
                        driver.fetch("http://example.com/", "127.0.0.1", port, timeout=15)
                        refused = "loaded"
                    except HttpError as exc:
                        refused = str(exc)
                    return page, refused
                finally:
                    driver.close()

            page, refused = await asyncio.to_thread(browse)
            assert page.status == 200
            assert page.bytes_received > 4000
            # The lab only serves *.uqab: a real site must fail, never go direct.
            assert refused != "loaded"
        finally:
            await stack.stop()

    run(scenario(), timeout=120)
