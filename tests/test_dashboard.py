"""Dashboard tests: view model, rendering and the live HTTP server (FR-25)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from config import Config
from dashboard.data import DashboardModel, load_model, run_detail, summary_text
from dashboard.render import (
    comparison_table,
    error_page,
    f1_chart,
    latency_chart,
    overview,
    personas_table,
    run_page,
    runs_table,
    status_cards,
)
from dashboard.server import start_background
from storage.db import RunRecord, db_session, record_run


def seed(cfg: Config) -> None:
    """Write three fake runs so the dashboard has something to show."""
    with db_session(Path(cfg.path("paths.sqlite"))) as connection:
        for index, config in enumerate(("A", "B", "C")):
            record_run(
                connection,
                RunRecord(
                    run_id=f"2026-01-0{index + 1}-{config}",
                    started_at=f"2026-01-0{index + 1}T09:0{index}:00",
                    configuration=config,
                    persona_id="balanced" if config != "C" else "high",
                    duration_s=60.0 + index,
                    real_bytes=200_000,
                    cover_bytes=400_000,
                    overhead=1.0 + index * 0.4,
                    notes="seeded",
                ),
                page_loads=[
                    {
                        "index": n,
                        "site": "demo.uqab",
                        "status": 200 if n != 3 else 500,
                        "bytes": 4096,
                        "load_ms": 150.0 + index * 120 + n,
                        "error": "" if n != 3 else "refused",
                    }
                    for n in range(1, 5)
                ],
                scores={
                    "folds": 5,
                    "lr": {"accuracy": 0.8 - index * 0.1, "precision": 0.8, "recall": 0.8,
                           "f1": 0.8 - index * 0.1, "f1_ci_low": 0.7 - index * 0.1,
                           "f1_ci_high": 0.9 - index * 0.1},
                    "rf": {"accuracy": 0.85 - index * 0.1, "precision": 0.85, "recall": 0.85,
                           "f1": 0.85 - index * 0.1, "f1_ci_low": 0.75 - index * 0.1,
                           "f1_ci_high": 0.95 - index * 0.1},
                },
                slot_timing={"p50_error_ms": 1.2, "p95_error_ms": 2.4, "max_error_ms": 3.1},
            )


def test_model_without_database(cfg: Config) -> None:
    model = load_model(cfg)
    assert model.db_error
    assert model.comparison == []
    html = overview(model)
    assert "UQAB" in html
    assert "no experiments recorded yet" in html
    assert "No comparison data yet." in html
    assert "No runs yet." in html


def test_model_reads_runs_and_stats(cfg: Config) -> None:
    seed(cfg)
    stats = {
        "t": 1.0,
        "state": "connected",
        "persona": "high",
        "period": "active",
        "real_up": 2048,
        "real_down": 4096,
        "cover_down": 8192,
        "overhead": 2.0,
        "session_started": True,
    }
    stats_path = cfg.path("paths.run") / "stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(json.dumps(stats), encoding="utf-8")

    model = load_model(cfg)
    assert not model.db_error
    assert [row["configuration"] for row in model.comparison] == ["A", "B", "C"]
    assert len(model.runs) == 3
    assert model.running
    assert model.personas, "the static personas should be discovered"
    ids = {persona["id"] for persona in model.personas}
    assert {"balanced", "high", "constant"} <= ids

    html = overview(model)
    assert "Configurations" in html
    assert "connected" in html
    assert "demo.uqab" not in html  # no page URLs leak onto the overview
    assert "<svg" in html
    assert "2.00x" in html


def test_charts_and_tables_render() -> None:
    rows: list[dict[str, Any]] = [
        {"configuration": "A", "runs": 1, "loads": 4, "p50_ms": 100.0, "overhead": 1.0,
         "accuracy": 0.9, "f1": 0.9, "f1_ci_low": 0.8, "f1_ci_high": 0.95},
        {"configuration": "C", "runs": 1, "loads": 4, "p50_ms": 400.0, "overhead": 2.5,
         "accuracy": 0.5, "f1": 0.5, "f1_ci_low": 0.3, "f1_ci_high": 0.7},
    ]
    table = comparison_table(rows)
    assert "A" in table and "C" in table
    assert "0.900" in table
    assert "width:90.0%" in table
    assert "svg" in f1_chart(rows)
    assert "svg" in latency_chart(rows)
    assert f1_chart([]) == ""
    assert latency_chart([]) == ""
    assert runs_table([]).startswith("<p")
    assert personas_table([]).startswith("<p")


def test_html_is_escaped() -> None:
    rows = [
        {
            "configuration": "<script>alert(1)</script>",
            "runs": 1,
            "loads": 1,
            "p50_ms": 1,
            "overhead": 1,
            "accuracy": 1,
            "f1": 1,
            "f1_ci_low": 1,
            "f1_ci_high": 1,
        }
    ]
    html = comparison_table(rows)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in error_page("<b>boom</b>")


def test_status_cards_and_summary(cfg: Config) -> None:
    seed(cfg)
    model = load_model(cfg)
    cards = status_cards(model)
    assert "Session" in cards
    assert "Personas" in cards
    text = summary_text(model)
    assert "Configuration A" in text
    assert "95% CI" in text


def test_run_detail_and_page(cfg: Config) -> None:
    seed(cfg)
    detail = run_detail(cfg, "2026-01-01-A")
    assert detail is not None
    assert detail["configuration"] == "A"
    assert len(detail["page_loads"]) == 4
    assert len(detail["scores"]) == 2
    assert detail["slot_timing"]["p50_error_ms"] == 1.2
    html = run_page(detail)
    assert "2026-01-01-A" in html
    assert "logreg" in html or "random_forest" in html
    assert "slot timing" in html.lower()
    assert run_detail(cfg, "nope") is None


def test_dashboard_serves_pages(cfg: Config) -> None:
    seed(cfg)
    server, thread = start_background(cfg, port=0)
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    base = f"http://{host}:{port}"
    try:
        def get(path: str) -> tuple[int, str, str]:
            with urllib.request.urlopen(base + path, timeout=10) as response:
                return (
                    response.status,
                    response.headers.get("Content-Type", ""),
                    response.read().decode("utf-8"),
                )

        status, content_type, body = get("/")
        assert status == 200
        assert content_type.startswith("text/html")
        assert "Configurations" in body

        status, _, css = get("/style.css")
        assert status == 200 and "--bg" in css or "background" in css

        status, content_type, body = get("/api/model")
        payload = json.loads(body)
        assert content_type.startswith("application/json")
        assert len(payload["runs"]) == 3

        status, _, body = get("/api/comparison")
        assert [row["configuration"] for row in json.loads(body)["configurations"]] == ["A", "B", "C"]

        status, _, body = get("/api/stats")
        assert status == 200

        status, _, body = get("/api/health")
        health = json.loads(body)
        assert health["ok"] is True and health["service"] == "dashboard"

        status, _, body = get("/run?id=2026-01-02-B")
        assert status == 200 and "2026-01-02-B" in body

        status, _, body = get("/runs")
        assert status == 200 and "2026-01-01-A" in body

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            get("/run?id=missing")
        assert excinfo.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            get("/nope")
        assert excinfo.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_model_to_dict_is_json_ready(cfg: Config) -> None:
    seed(cfg)
    payload = load_model(cfg).to_dict()
    assert json.loads(json.dumps(payload))["runs"]
    assert isinstance(DashboardModel().to_dict()["comparison"], list)
