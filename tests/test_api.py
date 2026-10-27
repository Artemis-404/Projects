"""Control API and engine tests (FR-27 to FR-30)."""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app_api.consent import CONSENT_TEXT_VERSION, ConsentStore
from app_api.engine import Engine, EngineBusy, UnknownPersona
from app_api.server import ApiServer, ControlAPI, EngineRunner
from config import Config, load_config
from experiments.socks_http import fetch
from tests.conftest import run
from tests.support import free_port

ORIGIN = "https://app.uqab"


@pytest.fixture()
def api(cfg: Config) -> Iterator[tuple[Engine, EngineRunner, ControlAPI]]:
    cfg = cfg.with_overrides(**{"tor.socks_port": free_port(), "tor.mode": "simulated"})
    engine = Engine(cfg)
    runner = EngineRunner(engine)
    api = ControlAPI(engine, runner, cfg)
    runner.start()
    yield engine, runner, api
    runner.shutdown()


def call(
    api: ControlAPI,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    origin: str | None = ORIGIN,
) -> tuple[int, dict[str, Any]]:
    return api.handle(method, path, payload, origin=origin)


def test_requests_without_the_app_origin_are_refused(api: tuple[Engine, EngineRunner, ControlAPI]) -> None:
    _, _, control = api
    status, _body = call(control, "GET", "/status", origin=None)
    assert status == 403
    status, _body = call(control, "GET", "/status", origin="https://evil.example")
    assert status == 403
    status, body = call(control, "GET", "/status")
    assert status == 200
    assert body["state"] == "stopped"


def test_personas_lists_only_validated_personas(api: tuple[Engine, EngineRunner, ControlAPI]) -> None:
    _, _, control = api
    status, body = call(control, "GET", "/personas")
    assert status == 200
    ids = {row["id"] for row in body["personas"]}
    assert {"low", "balanced", "high", "constant"} <= ids
    for row in body["personas"]:
        assert set(row) >= {"id", "source", "description"}


def test_start_rejects_unknown_persona(api: tuple[Engine, EngineRunner, ControlAPI]) -> None:
    _, _, control = api
    status, body = call(control, "POST", "/start", {"persona": "ghost"})
    assert status == 422
    assert body["error"] == "invalid_persona"


def test_start_requires_a_persona(api: tuple[Engine, EngineRunner, ControlAPI]) -> None:
    _, _, control = api
    status, body = call(control, "POST", "/start", {})
    assert status == 400


def test_unknown_route_and_method(api: tuple[Engine, EngineRunner, ControlAPI]) -> None:
    _, _, control = api
    status, _body = call(control, "GET", "/nope")
    assert status == 404
    status, _body = call(control, "PUT", "/status")
    assert status == 404


def test_consent_flow_and_profile_deletion(cfg: Config, api: tuple[Engine, EngineRunner, ControlAPI]) -> None:
    _, _, control = api
    status, body = call(control, "GET", "/consent")
    assert status == 200
    assert body["opt_in"] is False
    assert "consent" in body["text"].lower()

    status, body = call(control, "POST", "/consent", {"accept": False})
    assert status == 400

    status, body = call(control, "POST", "/consent", {"accept": True, "text_version": CONSENT_TEXT_VERSION})
    assert status == 200
    assert body["opt_in"] is True
    assert body["consented_at"]

    store = ConsentStore.from_config(cfg)
    assert store.is_opted_in() is True
    store.profile_path.parent.mkdir(parents=True, exist_ok=True)
    store.profile_path.write_text('{"version": 1}', encoding="utf-8")
    assert store.profile_exists()

    status, body = call(control, "DELETE", "/consent")
    assert status == 200
    assert body["opt_in"] is False
    assert not store.profile_exists()
    assert store.is_opted_in() is False


def test_consent_version_mismatch_is_rejected(api: tuple[Engine, EngineRunner, ControlAPI]) -> None:
    _, _, control = api
    status, _body = call(control, "POST", "/consent", {"accept": True, "text_version": "0.1"})
    assert status == 400


def test_full_session_lifecycle(
    cfg: Config, api: tuple[Engine, EngineRunner, ControlAPI]
) -> None:
    engine, _runner, control = api
    status, body = call(control, "POST", "/start", {"persona": "high"})
    assert status == 202, body
    assert body["state"] in {"connecting", "connected"}
    assert body["persona"] == "high"

    deadline = time.time() + 90
    while time.time() < deadline:
        status, body = call(control, "GET", "/status")
        if body["state"] == "connected":
            break
        time.sleep(0.2)
    assert body["state"] == "connected", body
    assert body["bootstrap"] == 100

    # The persona is locked while a session is running (FR-28).
    status, body = call(control, "POST", "/start", {"persona": "low"})
    assert status == 409

    # Traffic through the lab SOCKS port must show up in the counters (FR-29).
    ready = json.loads((cfg.path("paths.run") / "lab.json").read_text())
    socks_port = int(ready["socks_port"])

    async def load_page() -> None:
        for path in ("/", "/docs"):
            await fetch(f"http://demo.uqab{path}", "127.0.0.1", socks_port, timeout=30)

    run(load_page(), timeout=120)
    deadline = time.time() + 15
    counters = call(control, "GET", "/counters")[1]
    while time.time() < deadline:
        counters = call(control, "GET", "/counters")[1]
        if counters["real_up"] > 0 and counters["real_down"] > 0:
            break
        time.sleep(0.2)
    assert counters["real_up"] > 0
    assert counters["real_down"] > 0
    assert counters["cover_up"] > 0
    assert counters["overhead"] > 0
    assert counters["period"]

    status, body = call(control, "POST", "/stop")
    assert status == 202
    assert body["state"] == "stopped"
    status, body = call(control, "GET", "/status")
    assert body["state"] == "stopped"
    assert call(control, "GET", "/counters")[1]["real_up"] == 0


def test_http_layer_enforces_origin_and_serves_json(cfg: Config) -> None:
    cfg = cfg.with_overrides(**{"tor.mode": "simulated", "tor.socks_port": free_port()})
    engine = Engine(cfg)
    runner = EngineRunner(engine)
    control = ControlAPI(engine, runner, cfg)
    runner.start()
    server = ApiServer(("127.0.0.1", 0), control)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/status")
        response = conn.getresponse()
        assert response.status == 403
        response.read()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/health", headers={"Origin": ORIGIN})
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["ok"] is True

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/consent",
            body=b"{not json}",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        assert response.status == 400
        response.read()
    finally:
        server.shutdown()
        server.server_close()
        runner.shutdown()


def test_engine_reports_busy_and_stops_cleanly(cfg: Config) -> None:
    cfg = cfg.with_overrides(**{"tor.mode": "simulated", "tor.socks_port": free_port()})
    engine = Engine(cfg)
    runner = EngineRunner(engine)
    runner.start()
    try:
        runner.call(lambda: engine.start("constant"))
        with pytest.raises(EngineBusy):
            runner.call(lambda: engine.start("low"))
        with pytest.raises(UnknownPersona):
            runner.call(lambda: engine._persona("nope"))
        runner.call(engine.stop)
        assert engine.status_dict()["state"] == "stopped"
    finally:
        runner.shutdown()


def test_engine_writes_effective_config_for_children(cfg: Config) -> None:
    cfg = cfg.with_overrides(**{"tor.mode": "simulated"})
    engine = Engine(cfg)
    path = cfg.path("paths.run") / "effective-config.yaml"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert str(cfg.path("paths.run")) in text
    assert "profiles_dir" in text
    assert Path(engine.stats_path).name == "stats.json"


# ------------------------------------------------------------------- setup API


def test_tor_endpoint_reports_detection(
    api: tuple[Engine, EngineRunner, ControlAPI],
) -> None:
    _engine, _runner, control = api
    status, body = call(control, "GET", "/tor")
    assert status == 200
    assert "found" in body and "hint" in body
    assert body["found"] is False or isinstance(body["path"], str)


def test_setup_endpoint_summarises_effective_config(
    cfg: Config, api: tuple[Engine, EngineRunner, ControlAPI]
) -> None:
    _engine, _runner, control = api
    status, body = call(control, "GET", "/setup")
    assert status == 200
    assert body["socks_port"] > 0
    assert body["bridge"]["port"] == cfg.get_int("bridge.pt_port", 9443)
    assert body["persona_default"]
    assert body["tor"]["platform"] in {"windows", "posix"}


def test_parse_assignments_handles_types() -> None:
    from app_api.server import parse_assignments

    parsed = parse_assignments(
        [
            "tor.mode=external",
            "tor.socks_port=9150",
            "tor.bridges=[\"192.0.2.1:9443 abc\"]",
            "llm.variants=2",
            "tor.binary=C:/Tor/tor.exe",
        ]
    )
    assert parsed["tor.mode"] == "external"
    assert parsed["tor.socks_port"] == 9150
    assert parsed["tor.bridges"] == ["192.0.2.1:9443 abc"]
    assert parsed["llm.variants"] == 2
    assert parsed["tor.binary"] == "C:/Tor/tor.exe"
    with pytest.raises(ValueError):
        parse_assignments(["nonsense"])


def test_settings_file_and_cli_overrides_apply(tmp_path: Path) -> None:
    from app_api.server import load_settings_file

    settings = tmp_path / "settings.json"
    settings.write_text(
        '{"tor.mode": "external", "tor.socks_port": 9511, "persona.default": "high"}',
        encoding="utf-8",
    )
    assert load_settings_file(settings)["tor.socks_port"] == 9511

    cfg = load_config()
    merged = cfg.with_overrides(**load_settings_file(settings))
    assert merged.get_str("tor.mode") == "external"
    assert merged.get_int("tor.socks_port") == 9511
    assert merged.get_str("persona.default") == "high"

    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings_file(bad)


def test_gui_mode_can_start_a_lab_session_with_overrides(cfg: Config, tmp_path: Path) -> None:
    """The setup screen's defaults (auto mode) must produce a working session."""
    from app_api.engine import Engine
    from app_api.server import ControlAPI, EngineRunner

    settings = {"tor.mode": "simulated", "tor.socks_port": 0, "persona.default": "constant"}
    merged = cfg.with_overrides(**settings, **{"paths.run": str(tmp_path / "run2")})
    engine = Engine(merged)
    runner = EngineRunner(engine)
    runner.start()
    api = ControlAPI(engine, runner, merged)
    try:
        status, body = api.handle(
            "POST", "/start", {"persona": "constant"}, origin=merged.get_str("app_api.allowed_origin")
        )
        assert status == 202
        deadline = time.time() + 40
        state = body.get("state")
        while time.time() < deadline:
            _, status_body = api.handle(
                "GET", "/status", None, origin=merged.get_str("app_api.allowed_origin")
            )
            state = status_body.get("state")
            if state in {"connected", "blocked"}:
                break
            time.sleep(0.5)
        assert state in {"connected", "blocked"}
        if state == "connected":
            assert status_body["mode"] == "simulated"
            assert status_body["socks_port"] > 0
        api.handle("POST", "/stop", {}, origin=merged.get_str("app_api.allowed_origin"))
    finally:
        runner.shutdown()
