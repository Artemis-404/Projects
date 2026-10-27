"""Evaluation pipeline tests: classifiers, runner, storage, A/B/C comparison."""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

import pytest

from analysis.classifier import (
    ClassifierError,
    DecisionTree,
    LogisticRegression,
    RandomForest,
    bootstrap_f1_ci,
    evaluate,
    labelled_rows,
    metrics_for,
    stratified_folds,
)
from analysis.features import FeatureWindow, extract_windows
from analysis.pcap import build_packet, read_pcap, write_pcap
from config import Config
from experiments.capture import (
    SlotRecord,
    plain_to_packets,
    read_plain_log,
    read_slot_log,
    to_packets,
)
from experiments.compare import format_table, run_comparison
from experiments.run import timeline_from_config
from experiments.runner import Phase, Timeline, replay, summarise
from experiments.socks_http import PageLoad
from storage.db import (
    RunRecord,
    comparison,
    db_session,
    export_comparison,
    open_db,
    page_loads,
    record_run,
    record_slot_timing,
    runs,
    scores,
)


def labelled_windows(seed: int = 11, count: int = 120) -> list[FeatureWindow]:
    """Synthetic windows: ACTIVE has more bytes and shorter gaps than IDLE."""
    rng = random.Random(seed)
    windows: list[FeatureWindow] = []
    for index in range(count):
        active = index % 2 == 0
        packets = rng.randint(8, 14) if active else rng.randint(1, 3)
        size = rng.uniform(500, 1200) if active else rng.uniform(900, 1100)
        windows.append(
            FeatureWindow(
                start=float(index),
                end=float(index + 1),
                packets=packets,
                bytes=int(packets * size),
                mean_size=size,
                var_size=rng.uniform(0, 400) if active else rng.uniform(0, 50),
                mean_gap_ms=rng.uniform(60, 120) if active else rng.uniform(300, 500),
                var_gap_ms=rng.uniform(0, 4000) if active else rng.uniform(0, 500),
                up_ratio=rng.uniform(0.3, 0.7) if active else rng.uniform(0.0, 0.1),
                bursts=rng.randint(1, 4) if active else 0,
                label="active" if active else "idle",
            )
        )
    return windows


# --------------------------------------------------------------------- metrics


def test_metrics_basics() -> None:
    result = metrics_for([1, 1, 0, 0], [1, 0, 0, 1])
    assert result.accuracy == 0.5
    assert result.precision == 0.5
    assert result.recall == 0.5
    assert result.f1 == 0.5
    with pytest.raises(ClassifierError):
        metrics_for([1], [1, 0])
    empty = metrics_for([1, 1], [1, 1])
    assert empty.f1 == 1.0


def test_stratified_folds_keep_every_index_once() -> None:
    labels = [1, 0] * 19 + [1]
    buckets = stratified_folds(labels, 5, seed=3)
    seen = [index for bucket in buckets for index in bucket]
    assert sorted(seen) == list(range(len(labels)))
    assert len(buckets) == 5


def test_single_class_is_rejected() -> None:
    with pytest.raises(ClassifierError):
        labelled_rows([_window(0, label="idle")])


def _window(index: int, *, label: str) -> FeatureWindow:
    return FeatureWindow(
        start=float(index),
        end=float(index + 1),
        packets=5,
        bytes=1000,
        mean_size=220.0,
        var_size=10.0,
        mean_gap_ms=120.0,
        var_gap_ms=8.0,
        up_ratio=0.5,
        bursts=1,
        label=label,
    )


def test_models_learn_a_separable_problem() -> None:
    windows = labelled_windows()
    rows, labels = labelled_rows(windows)
    for model in (LogisticRegression(iterations=300), RandomForest(n_trees=12, seed=2)):
        model.fit(rows, labels)
        predictions = model.predict(rows)
        assert sum(p == t for p, t in zip(predictions, labels, strict=True)) / len(labels) > 0.9


def test_decision_tree_finds_a_split() -> None:
    tree = DecisionTree(max_depth=3, min_samples=2, seed=1)
    rows = [[float(i)] for i in range(20)]
    labels = [0] * 10 + [1] * 10
    tree.fit(rows, labels)
    assert tree.predict_one([1.0]) == 0
    assert tree.predict_one([19.0]) == 1


def test_evaluate_reports_f1_with_confidence_interval() -> None:
    result = evaluate(labelled_windows(), folds=5, seed=5, bootstrap=200)
    assert result.windows == 120
    assert result.active_windows == 60
    assert result.lr.f1 > 0.8
    assert result.rf.f1 > 0.8
    low, high = result.lr_f1_ci
    assert 0.0 <= low <= result.lr.f1 + 1e-9
    assert result.lr.f1 - 1e-9 <= high <= 1.0
    payload = result.to_dict()
    assert payload["lr"]["f1_ci_low"] == low
    assert payload["features"]


def test_bootstrap_interval_is_ordered() -> None:
    low, high = bootstrap_f1_ci([1, 0, 1, 0] * 10, [1, 0, 1, 1] * 10, samples=200)
    assert 0.0 <= low <= high <= 1.0


# ---------------------------------------------------------------------- runner


def test_timeline_from_config_builds_alternating_phases(cfg: Config) -> None:
    timeline = timeline_from_config(cfg, loads=3)
    assert len(timeline.phases) == 6
    assert [phase.kind for phase in timeline.phases] == ["load", "idle"] * 3
    assert timeline.total_s > 0
    labels = timeline.labels()
    assert labels[0][2] == "active"
    assert labels[1][2] == "idle"


class FakeFetcher:
    def __init__(self, *, fail_on: str = "") -> None:
        self.calls: list[tuple[str, str, int, float]] = []
        self.fail_on = fail_on

    def __call__(
        self, url: str, host: str, port: int, *, timeout: float = 30.0
    ) -> PageLoad:
        self.calls.append((url, host, port, timeout))
        if self.fail_on and self.fail_on in url:
            raise RuntimeError("boom")
        return PageLoad(url=url, status=200, bytes_received=4096, load_ms=12.0)


def test_replay_times_every_load_and_labels_phases() -> None:
    timeline = Timeline(
        phases=[
            Phase(kind="load", duration_s=0.2, url="http://demo.uqab/", site="demo.uqab", label="active"),
            Phase(kind="idle", duration_s=0.2, label="idle"),
        ]
    )
    fetcher = FakeFetcher()
    outcome = replay(timeline, proxy_host="127.0.0.1", proxy_port=1234, fetch_fn=fetcher)
    assert len(outcome.loads) == 1
    load = outcome.loads[0]
    assert load.status == 200
    assert load.bytes == 4096
    assert load.ok
    assert fetcher.calls[0][1:3] == ("127.0.0.1", 1234)
    assert [row["label"] for row in outcome.phases] == ["active", "idle"]
    assert outcome.phases[1]["t_end"] >= outcome.phases[0]["t_end"]
    # The ground truth and the capture share one wall-clock origin.
    assert outcome.started_wall > 1_600_000_000
    assert outcome.started_mono > 0


def test_replay_records_failures_without_stopping() -> None:
    timeline = Timeline(
        phases=[
            Phase(kind="load", duration_s=0.1, url="http://demo.uqab/ok", label="active"),
            Phase(kind="load", duration_s=0.1, url="http://demo.uqab/fail", label="active"),
            Phase(kind="idle", duration_s=0.1, label="idle"),
        ]
    )
    outcome = replay(
        timeline,
        proxy_host="127.0.0.1",
        proxy_port=1,
        fetch_fn=FakeFetcher(fail_on="/fail"),
    )
    assert len(outcome.loads) == 2
    assert outcome.loads[0].ok
    assert not outcome.loads[1].ok
    assert "boom" in outcome.loads[1].error


def test_summary_statistics() -> None:
    timeline = Timeline(phases=[Phase(kind="load", duration_s=0.1, url="http://a/")] * 2)
    outcome = replay(timeline, proxy_host="h", proxy_port=1, fetch_fn=FakeFetcher())
    summary = summarise(outcome.loads)
    assert summary["loads"] == 2
    assert summary["ok"] == 2
    assert summary["bytes"] == 8192
    # Latency is measured by the runner, not copied from the page-load object.
    assert 0.0 <= summary["p50_ms"] < 1000.0


# --------------------------------------------------------------------- capture


def test_slot_log_becomes_packets(tmp_path: Path) -> None:
    log = tmp_path / "slots.jsonl"
    log.write_text(
        "\n".join(
            json.dumps({"t": 100.0 + i, "index": i, "size": 512 + i, "state": "active"})
            for i in range(5)
        )
        + "\n",
        encoding="utf-8",
    )
    records, offset = read_slot_log(log)
    assert len(records) == 5
    assert offset > 0
    packets = to_packets(records, t0=100.0, base_ts=5000.0)
    assert [round(p.ts, 3) for p in packets] == [5000.0, 5001.0, 5002.0, 5003.0, 5004.0]
    assert all(p.dport == 9443 for p in packets)
    # Reading from the saved offset returns only new records.
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"t": 200.0, "index": 9, "size": 64}) + "\n")
    extra, _ = read_slot_log(log, offset=offset)
    assert len(extra) == 1


def test_missing_slot_log_is_empty(tmp_path: Path) -> None:
    records, offset = read_slot_log(tmp_path / "nope.jsonl")
    assert records == []
    assert offset == 0


def test_plain_log_directions(tmp_path: Path) -> None:
    log = tmp_path / "plain.jsonl"
    log.write_text(
        json.dumps({"t": 10.0, "index": 1, "size": 100, "dir": "up"})
        + "\n"
        + json.dumps({"t": 10.5, "index": 2, "size": 900, "dir": "down"})
        + "\n",
        encoding="utf-8",
    )
    records, _ = read_plain_log(log)
    assert [record.direction for record in records] == ["up", "down"]
    packets = plain_to_packets(records, t0=10.0, base_ts=0.0)
    assert packets[1].src == "10.0.0.9"
    assert packets[0].src == "10.0.0.2"


def test_lab_style_capture_feeds_the_features(tmp_path: Path) -> None:
    records = [
        SlotRecord(t=1000.0 + index * 0.05, index=index, size=1024)
        for index in range(40)
    ]
    packets = to_packets(records, t0=1000.0, base_ts=100.0)
    path = tmp_path / "cap.pcap"
    write_pcap(path, [(p.ts, build_packet(p.src, p.dst, p.sport, p.dport, p.length - 54)) for p in packets])
    windows = extract_windows(read_pcap(path), window_s=1.0, bridge_port=9443)
    assert len(windows) == 2
    assert all(window.bytes > 0 for window in windows)


# --------------------------------------------------------------------- storage


def test_database_roundtrip_and_comparison(tmp_path: Path) -> None:
    db_path = tmp_path / "uqab.sqlite"
    connection = open_db(db_path)
    try:
        for index, config in enumerate(("A", "B", "C")):
            run_id = f"run-{config}"
            record_run(
                connection,
                RunRecord(
                    run_id=run_id,
                    started_at=f"2026-01-0{index + 1}T10:00:00",
                    configuration=config,
                    persona_id="balanced",
                    duration_s=12.0 + index,
                    real_bytes=100_000,
                    cover_bytes=200_000 + index * 10_000,
                    overhead=2.0 + index * 0.5,
                    notes="test",
                ),
                page_loads=[
                    {"index": 1, "site": "demo.uqab", "status": 200, "bytes": 4096, "load_ms": 200.0 + index * 50},
                    {"index": 2, "site": "demo.uqab", "status": 500, "bytes": 0, "load_ms": 1.0, "error": "x"},
                ],
                scores={
                    "folds": 5,
                    "lr": {"accuracy": 0.7, "precision": 0.7, "recall": 0.7, "f1": 0.7,
                           "f1_ci_low": 0.6, "f1_ci_high": 0.8},
                    "rf": {"accuracy": 0.75, "precision": 0.75, "recall": 0.75, "f1": 0.75,
                           "f1_ci_low": 0.65, "f1_ci_high": 0.85},
                },
                slot_timing={"p50_error_ms": 1.0, "p95_error_ms": 2.0, "max_error_ms": 3.0},
            )
        assert len(runs(connection)) == 3
        assert len(runs(connection, "B")) == 1
        assert len(page_loads(connection, "run-A")) == 2
        assert len(scores(connection, "run-A")) == 2

        # Re-recording the same run replaces its children instead of duplicating.
        record_run(
            connection,
            RunRecord(run_id="run-A", started_at="2026-01-01T10:00:00", configuration="A",
                      persona_id="balanced", overhead=2.0),
            page_loads=[{"index": 1, "site": "demo.uqab", "status": 200, "bytes": 1, "load_ms": 5.0}],
            scores={"folds": 5, "lr": {"f1": 0.5}},
        )
        assert len(page_loads(connection, "run-A")) == 1
        record_slot_timing(connection, "run-B", {"p50_error_ms": 9, "p95_error_ms": 9, "max_error_ms": 9})

        summaries = comparison(connection)
        assert [summary.configuration for summary in summaries] == ["A", "B", "C"]
        assert summaries[1].loads == 1
        assert summaries[1].f1 > 0
    finally:
        connection.close()

    with db_session(db_path) as connection:
        out = export_comparison(connection, tmp_path / "comparison-db.json")
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["configurations"]) == 3


def test_format_table_lists_every_configuration(tmp_path: Path) -> None:
    db_path = tmp_path / "uqab.sqlite"
    connection = open_db(db_path)
    try:
        for config in ("A", "C"):
            record_run(
                connection,
                RunRecord(run_id=f"r-{config}", started_at="2026-01-01T00:00:00",
                          configuration=config, persona_id="balanced", overhead=1.5),
            )
        table = format_table(comparison(connection))
    finally:
        connection.close()
    assert "cfg" in table
    assert "A" in table and "C" in table
    assert "F1" in table


def test_compare_runs_all_configurations(tmp_path: Path, cfg: Config) -> None:
    """Full A/B/C pipeline on the lab stack with a tiny timeline (slow test)."""
    timeline = Timeline(
        phases=[
            Phase(kind="load", duration_s=0.4, url="http://demo.uqab/", site="demo.uqab", label="active"),
            Phase(kind="idle", duration_s=1.6, label="idle"),
            Phase(kind="load", duration_s=0.4, url="http://demo.uqab/docs", site="demo.uqab", label="active"),
            Phase(kind="idle", duration_s=1.6, label="idle"),
        ]
    )
    result = run_comparison(
        cfg=cfg,
        configurations=("A", "C"),
        timeline=timeline,
        out_dir=tmp_path / "experiments",
    )
    assert result.ok, result.notes
    assert [r.configuration for r in result.results] == ["A", "C"]
    assert all(r.summary["ok"] == 2 for r in result.results)
    written = result.write(tmp_path / "experiments")
    assert written["csv"].is_file()
    with written["csv"].open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["configuration"] for row in rows} == {"A", "C"}
    assert result.results[0].windows >= 1


# ---------------------------------------------------------------------- report


def test_report_renders_the_comparison(tmp_path: Path, cfg: Config) -> None:
    from experiments.report import collect, interpretation, render, write_report

    seed = Path(cfg.path("paths.sqlite"))
    with db_session(seed) as connection:
        for index, config in enumerate(("A", "B", "C")):
            record_run(
                connection,
                RunRecord(
                    run_id=f"2026-02-0{index + 1}-{config}",
                    started_at="2026-02-01T10:00:00",
                    configuration=config,
                    persona_id="balanced" if config != "C" else "high",
                    duration_s=90.0,
                    real_bytes=120_000,
                    cover_bytes=300_000,
                    overhead=1.0 + index * 0.75,
                    notes="seeded",
                ),
                page_loads=[
                    {"index": n, "site": "demo.uqab", "status": 200, "bytes": 4096, "load_ms": 200.0 + index * 300}
                    for n in range(1, 4)
                ],
                scores={
                    "folds": 5,
                    "lr": {"accuracy": 0.9 - index * 0.15, "precision": 0.9, "recall": 0.9,
                           "f1": 0.9 - index * 0.15, "f1_ci_low": 0.8 - index * 0.15,
                           "f1_ci_high": 0.98 - index * 0.15},
                    "rf": {"accuracy": 0.92 - index * 0.15, "precision": 0.92, "recall": 0.92,
                           "f1": 0.92 - index * 0.15, "f1_ci_low": 0.82 - index * 0.15,
                           "f1_ci_high": 0.99 - index * 0.15},
                },
                slot_timing={"p50_error_ms": 0.8, "p95_error_ms": 1.6, "max_error_ms": 2.2},
            )

    data = collect(cfg)
    assert data.statistics["runs"] == 3
    assert data.statistics["page_loads"] == 9
    assert [row["configuration"] for row in data.configurations] == ["A", "B", "C"]
    text = interpretation(data)
    assert "Plain Tor" in text and "persona cover" in text

    markdown = render(data)
    assert "| Config | Meaning |" in markdown
    assert "plain Tor (no cover)" in markdown
    assert "constant-rate" in markdown
    assert "2026-02-03-C" in markdown
    assert "logreg" in markdown
    assert "Accuracy" in markdown
    assert "Classifier scores" in markdown

    written = write_report(cfg, tmp_path / "results.md")
    assert written.is_file()
    assert "UQAB evaluation results" in written.read_text(encoding="utf-8")
    assert written.with_suffix(".json").is_file()


def test_report_without_data_says_so(tmp_path: Path, cfg: Config) -> None:
    from experiments.report import collect, render

    empty = cfg.with_overrides(**{"paths.sqlite": str(tmp_path / "none.sqlite")})
    data = collect(empty)
    assert data.notes
    markdown = render(data)
    assert "no runs recorded" in markdown
    assert "cannot be" in markdown


def test_replay_jitter_keeps_labels_off_the_grid() -> None:
    """Phase lengths vary, but the ground truth always matches what happened."""
    timeline = Timeline(
        phases=[
            Phase(kind="load", duration_s=1.0, url="http://a/", label="active"),
            Phase(kind="idle", duration_s=2.0, label="idle"),
        ]
        * 3
    )
    outcome = replay(
        timeline,
        proxy_host="h",
        proxy_port=1,
        fetch_fn=FakeFetcher(),
        jitter_s=0.4,
        seed=3,
    )
    durations = [row["duration_s"] for row in outcome.phases]
    assert len(durations) == 6
    assert all(0.2 <= value <= 2.4 for value in durations)
    assert any(abs(value - 1.0) > 0.01 for value in durations)
    # The recorded interval is what the clock actually did.
    for row in outcome.phases:
        assert row["t_end"] - row["t_start"] >= row["duration_s"] - 0.05

    fixed = replay(timeline, proxy_host="h", proxy_port=1, fetch_fn=FakeFetcher())
    assert [row["duration_s"] for row in fixed.phases] == [1.0, 2.0, 1.0, 2.0, 1.0, 2.0]
