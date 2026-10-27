"""Local-LLM persona generation tests (FR-14, FR-15) against a stub endpoint."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from config import Config, load_config
from persona.llm import (
    LLMError,
    LLMSettings,
    PersonaClient,
    build_messages,
    extract_json,
    generate_personas,
    prepare,
    repair_weights,
    write_personas,
)
from persona.validator import Limits, validate_dict

BALANCED = json.loads(
    (Path(__file__).resolve().parent.parent / "persona" / "profiles" / "balanced.json").read_text(
        encoding="utf-8"
    )
)


def persona_reply(**overrides: Any) -> dict[str, Any]:
    data = json.loads(json.dumps(BALANCED))
    data.update({"source": "llm", "id": "llm-1", "description": "Generated persona"})
    data.update(overrides)
    return data


class StubEndpoint:
    """Tiny Ollama/OpenAI-shaped server returning scripted replies."""

    def __init__(self, replies: list[str], *, status: int = 200) -> None:
        self.replies = replies
        self.status = status
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"path": self.path, "body": body})
                text = outer.replies.pop(0) if outer.replies else ""
                if outer.status != 200:
                    payload = b'{"error":"stub failure"}'
                    self.send_response(outer.status)
                else:
                    payload = json.dumps(
                        {
                            "model": body.get("model", "stub"),
                            "message": {"role": "assistant", "content": text},
                            "choices": [{"message": {"role": "assistant", "content": text}}],
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:  # silence the test log
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> StubEndpoint:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        host = str(self.server.server_address[0])
        port = int(self.server.server_address[1])
        return f"http://{host}:{port}"


@pytest.fixture()
def limits() -> Limits:
    return Limits.from_config(load_config())


@pytest.fixture()
def stub() -> Iterator[StubEndpoint]:
    with StubEndpoint([json.dumps(persona_reply())]) as server:
        yield server


def test_build_messages_mentions_the_style() -> None:
    messages = build_messages("media", 0)
    assert messages[0]["role"] == "system"
    assert "JSON" in messages[0]["content"]
    assert "video" in messages[1]["content"]


def test_extract_json_handles_fences_and_prose() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(LLMError):
        extract_json("no json here")
    with pytest.raises(LLMError):
        extract_json("{not json}")


def test_repair_weights_normalises_and_drops_unknown() -> None:
    repaired = repair_weights(
        {
            "states": [{"name": "a"}, {"name": "b"}],
            "transitions": {
                "a": {"a": 3, "b": 1, "ghost": 5},
                "b": {"b": 2},
            },
        }
    )
    rows = repaired["transitions"]
    assert set(rows["a"]) == {"a", "b"}
    assert abs(sum(rows["a"].values()) - 1.0) < 1e-6
    assert abs(rows["a"]["a"] - 0.75) < 1e-6
    assert abs(sum(rows["b"].values()) - 1.0) < 1e-6


def test_prepare_forces_id_and_strips_urls() -> None:
    prepared = prepare(
        {"id": "evil", "source": "static", "description": "see https://example.com/x now"},
        persona_id="llm-2",
    )
    assert prepared["id"] == "llm-2"
    assert prepared["source"] == "llm"
    assert "https" not in prepared["description"]
    assert prepared["floor_slot_ms"] == 500


def test_client_generates_a_valid_persona(stub: StubEndpoint, limits: Limits) -> None:
    settings = LLMSettings(url=stub.url, model="stub", timeout_s=5.0, variants=1)
    client = PersonaClient(settings)
    result = client.generate(style="media", count=1, limits=limits)
    assert result.ok
    assert result.personas[0].source == "llm"
    assert validate_dict(result.personas[0].to_dict(), limits).ok
    assert stub.requests and stub.requests[0]["path"].startswith("/v1/chat/completions")


def test_retry_then_success(limits: Limits) -> None:
    with StubEndpoint(["not json at all", json.dumps(persona_reply())]) as stub:
        settings = LLMSettings(url=stub.url, model="stub", timeout_s=5.0, attempts=3)
        result = PersonaClient(settings).generate(style="reading", count=1, limits=limits)
    assert result.ok
    assert result.attempts == 2
    assert result.rejected


def test_invalid_persona_is_rejected(limits: Limits) -> None:
    bad = persona_reply(states=[])
    with StubEndpoint([json.dumps(bad)] * 4) as stub:
        settings = LLMSettings(url=stub.url, model="stub", timeout_s=5.0, attempts=2)
        result = PersonaClient(settings).generate(style="balanced", count=1, limits=limits)
    assert not result.ok
    assert result.fell_back
    assert result.attempts == 2


def test_unreachable_endpoint_falls_back(cfg: Config, tmp_path: Path) -> None:
    cfg = cfg.with_overrides(**{"llm.url": "http://127.0.0.1:9", "llm.timeout_s": 2,
                                "llm.out_dir": str(tmp_path / "generated")})
    result = generate_personas(cfg, style="balanced", count=1, limits=Limits.from_config(cfg))
    assert result.fell_back
    assert result.personas
    assert validate_dict(result.personas[0].to_dict(), Limits.from_config(cfg)).ok


def test_generation_writes_persona_files(cfg: Config, tmp_path: Path, limits: Limits) -> None:
    out_dir = tmp_path / "generated"
    with StubEndpoint([json.dumps(persona_reply())]) as stub:
        cfg = cfg.with_overrides(**{"llm.url": stub.url, "llm.out_dir": str(out_dir),
                                    "llm.variants": 1, "llm.timeout_s": 5})
        result = generate_personas(cfg, style="chatty", count=1, limits=limits)
    assert result.ok and not result.fell_back
    files = list(out_dir.glob("*.json"))
    assert len(files) == 1
    loaded = json.loads(files[0].read_text(encoding="utf-8"))
    assert loaded["source"] == "llm"
    assert loaded["id"] == "llm-1"


def test_server_error_reply_is_retried(limits: Limits) -> None:
    with StubEndpoint(["{}"], status=500) as stub:
        settings = LLMSettings(url=stub.url, model="stub", timeout_s=5.0, attempts=1)
        result = PersonaClient(settings).generate(style="balanced", count=1, limits=limits)
    assert not result.ok
    assert result.fell_back


def test_write_personas_roundtrip(tmp_path: Path, limits: Limits) -> None:
    persona = validate_dict(prepare(persona_reply(), persona_id="llm-9"), limits).persona
    paths = write_personas([persona], tmp_path / "gen")
    assert paths[0].is_file()
    assert json.loads(paths[0].read_text(encoding="utf-8"))["id"] == "llm-9"
