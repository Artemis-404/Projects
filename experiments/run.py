"""Experiment orchestration: one configuration, end to end (SDD 5.8, FR-20/21/23).

``run_experiment`` boots the lab stack (or uses an existing Tor SOCKS port),
replays the scripted browsing timeline, turns the shaped records into a capture,
extracts labelled windows, trains the classifiers, stores everything in SQLite
and writes a run summary.  Comparing A/B/C lives in ``experiments/compare.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from analysis.classifier import EvaluationResult, evaluate
from analysis.features import (
    FeatureWindow,
    GroundTruthWindow,
    extract_windows,
    write_features,
    write_ground_truth,
)
from analysis.pcap import read_pcap
from config import Config, load_config
from experiments.browser import replay_with_driver
from experiments.capture import (
    capture_from_logs,
    capture_from_plain_log,
    file_offset,
    write_capture,
)
from experiments.runner import (
    CONFIGURATION_NOTES,
    CONFIGURATIONS,
    ExperimentError,
    LoadResult,
    Timeline,
    port_open,
    replay,
    summarise,
)
from storage.db import RunRecord, open_db, record_run

#: Which persona each configuration uses; A runs plain Tor with no cover.
CONFIG_PERSONAS: dict[str, str | None] = {"A": None, "B": "constant", "C": "high"}


@dataclass(slots=True)
class RunResult:
    run_id: str
    configuration: str
    persona: str
    loads: list[LoadResult]
    summary: dict[str, Any]
    windows: int = 0
    active_windows: int = 0
    idle_windows: int = 0
    evaluation: EvaluationResult | None = None
    duration_s: float = 0.0
    artifacts: dict[str, str] = field(default_factory=dict)
    slot_timing: dict[str, float] | None = None
    error: str = ""
    driver: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "configuration": self.configuration,
            "persona": self.persona,
            "summary": self.summary,
            "windows": self.windows,
            "active_windows": self.active_windows,
            "idle_windows": self.idle_windows,
            "slot_timing": self.slot_timing,
            "evaluation": self.evaluation.to_dict() if self.evaluation else None,
            "duration_s": round(self.duration_s, 3),
            "artifacts": self.artifacts,
            "error": self.error,
            "driver": self.driver,
        }

    def write(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return target


def make_run_id(configuration: str, persona: str) -> str:
    """Unique per run: repeats of A/B/C can start inside the same second."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{time.time_ns() % 1_000_000_000:09d}-{configuration}-{persona}"


def config_for(name: str) -> tuple[str | None, str]:
    """Configuration letter -> (persona id, human description)."""
    key = name.strip().upper()
    if key not in CONFIGURATIONS:
        raise ExperimentError(f"unknown configuration {name!r}; expected A, B or C")
    return CONFIG_PERSONAS[key], CONFIGURATION_NOTES[key]


def timeline_from_config(cfg: Config, *, loads: int | None = None) -> Timeline:
    """Build the scripted timeline from ``experiments.sites`` (URLs or hosts)."""
    sites = cfg.get("experiments.sites", ["http://demo.uqab/"]) or ["http://demo.uqab/"]
    if isinstance(sites, str):
        sites = [sites]
    urls: list[str] = []
    for entry in sites:
        text = str(entry).strip()
        if not text:
            continue
        urls.append(text if "://" in text else f"http://{text}/")
    if not urls:
        urls = ["http://demo.uqab/"]
    count = int(loads if loads is not None else cfg.get_int("experiments.loads_per_site", 30))
    load_s = max(0.2, float(cfg.get_int("experiments.load_s", 1)))
    idle_s = max(0.2, float(cfg.get_int("experiments.idle_s", 3)))
    names = [url.split("://", 1)[-1].split("/", 1)[0] for url in urls]
    # ``count`` is a total page-load budget shared across the site list.
    return _timeline_from_urls(
        urls, site_names=names, load_s=load_s, idle_s=idle_s, total_loads=count
    )


def _timeline_from_urls(
    urls: list[str],
    *,
    site_names: list[str],
    load_s: float,
    idle_s: float,
    total_loads: int | None = None,
) -> Timeline:
    from experiments.runner import Phase

    phases: list[Phase] = []
    if total_loads is not None and urls:
        repeats = max(1, -(-int(total_loads) // len(urls)))  # ceil division
        urls = (urls * repeats)[: max(1, int(total_loads))]
        site_names = [url.split("://", 1)[-1].split("/", 1)[0] for url in urls]
    for index, url in enumerate(urls):
        site = site_names[index] if index < len(site_names) else url
        phases.append(Phase(kind="load", duration_s=load_s, url=url, site=site, label="active"))
        phases.append(Phase(kind="idle", duration_s=idle_s, site=site, label="idle"))
    return Timeline(phases=phases)


def _drive_lab(
    cfg: Config,
    persona: str | None,
    timeline: Timeline,
    *,
    socks_port: int,
    offsets: dict[str, int],
    jitter_s: float = 0.0,
    seed: int = 7,
    requests_per_load: int = 1,
    driver: str = "http",
) -> tuple[Any, list[LoadResult], list[dict[str, Any]], float, float, str]:
    """Start the lab stack, replay the timeline through ``driver``, stop the stack."""

    async def drive() -> tuple[Any, list[LoadResult], list[dict[str, Any]], float, float, str]:
        from lab.stack import LabStack

        stack = LabStack(cfg, persona=persona, socks_port=socks_port)
        await stack.start()
        try:
            # The browser lives in the replay thread (Playwright's sync API is
            # bound to the thread that started it).
            outcome, note = await asyncio.to_thread(
                replay_with_driver,
                replay,
                timeline,
                driver=driver,
                proxy_host="127.0.0.1",
                proxy_port=stack.socks_port,
                jitter_s=jitter_s,
                seed=seed,
                requests_per_load=requests_per_load,
            )
        finally:
            await stack.stop()
        return (
            stack,
            outcome.loads,
            outcome.phases,
            outcome.started_mono,
            outcome.started_wall,
            note,
        )

    return asyncio.run(drive())


def _windows_from_logs(
    cfg: Config,
    run_dir: Path,
    *,
    offsets: dict[str, int],
    t0: float,
    base_ts: float,
    phases: list[dict[str, Any]],
    window_s: float = 1.0,
    plain: bool = False,
) -> tuple[list[FeatureWindow], Path | None]:
    logs = cfg.path("paths.run") / "logs"
    if plain:
        packets = capture_from_plain_log(
            plain_log=logs / "plain-client.jsonl",
            offset=offsets.get("plain", 0),
            t0=t0,
            base_ts=base_ts,
        )
    else:
        packets = capture_from_logs(
            client_log=logs / "slots-client.jsonl",
            server_log=logs / "slots-server.jsonl",
            client_offset=offsets.get("client", 0),
            server_offset=offsets.get("server", 0),
            t0=t0,
            base_ts=base_ts,
        )
    if not packets:
        return [], None
    capture_path = run_dir / "capture.pcap"
    write_capture(capture_path, packets)
    truth = [
        GroundTruthWindow(
            start=base_ts + float(row["t_start"]),
            end=base_ts + float(row["t_end"]),
            label=str(row["label"]),
            site=str(row.get("site", "")),
            load_ms=float(row.get("load_ms", 0.0) or 0.0),
        )
        for row in phases
    ]
    windows = extract_windows(
        read_pcap(capture_path),
        window_s=window_s,
        bridge_port=int(cfg.get_int("bridge.pt_port", 9443)),
        ground_truth=truth,
    )
    return windows, capture_path


def run_experiment(
    configuration: str,
    *,
    cfg: Config | None = None,
    timeline: Timeline | None = None,
    persona: str | None = None,
    use_lab: bool = True,
    socks_host: str = "127.0.0.1",
    socks_port: int = 0,
    out_dir: Path | str | None = None,
    evaluate_after: bool = True,
    folds: int | None = None,
    seed: int | None = None,
) -> RunResult:
    """Run one configuration on one persona and persist the results."""
    cfg = cfg or load_config()
    letter = configuration.strip().upper()
    default_persona, _note = config_for(letter)
    persona_id = persona or default_persona or "balanced"
    run_id = make_run_id(letter, persona_id)
    target = Path(out_dir) if out_dir is not None else Path(cfg.path("paths.experiments"))
    run_dir = target / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    timeline = timeline or timeline_from_config(cfg)
    jitter_s = float(cfg.get_int("experiments.jitter_s", 400) or 0) / 1000.0
    seed = int(cfg.get_int("analysis.random_seed", 7) if seed is None else seed)
    requests_per_load = max(1, cfg.get_int("experiments.requests_per_load", 24))
    driver = cfg.get_str("experiments.driver", "auto") or "auto"
    driver_note = ""
    logs = cfg.path("paths.run") / "logs"
    plain_run = use_lab and default_persona is None and persona is None
    offsets = {
        "client": file_offset(logs / "slots-client.jsonl"),
        "server": file_offset(logs / "slots-server.jsonl"),
        "plain": file_offset(logs / "plain-client.jsonl"),
    }

    started = time.time()
    started_wall = time.time()
    t0 = time.monotonic()
    stack = None
    try:
        if use_lab:
            stack, loads, phases, t0, started_wall, driver_note = _drive_lab(
                cfg,
                None if plain_run else persona_id,
                timeline,
                socks_port=socks_port,
                offsets=offsets,
                jitter_s=jitter_s,
                seed=seed,
                requests_per_load=requests_per_load,
                driver=driver,
            )
        else:
            port = socks_port or int(cfg.get_int("tor.socks_port", 9150))
            if not port_open(socks_host, port, wait_s=30.0):
                raise ExperimentError(f"no SOCKS proxy on {socks_host}:{port}; is Tor running?")
            outcome, driver_note = replay_with_driver(
                replay,
                timeline,
                driver=driver,
                proxy_host=socks_host,
                proxy_port=port,
                jitter_s=jitter_s,
                seed=seed,
                requests_per_load=requests_per_load,
            )
            loads, phases = outcome.loads, outcome.phases
            t0, started_wall = outcome.started_mono, outcome.started_wall
    except Exception as exc:
        result = RunResult(
            run_id=run_id,
            configuration=letter,
            persona=persona_id,
            loads=[],
            summary=summarise([]),
            duration_s=time.time() - started,
            error=f"{type(exc).__name__}: {exc}",
            driver=driver_note,
        )
        result.write(run_dir / "summary.json")
        return result
    finally:
        del stack  # the driver already stopped it

    duration = time.time() - started
    write_ground_truth(
        run_dir / "ground-truth.csv",
        [
            GroundTruthWindow(
                start=float(row["t_start"]),
                end=float(row["t_end"]),
                label=str(row["label"]),
                site=str(row.get("site", "")),
                load_ms=float(row.get("load_ms", 0.0) or 0.0),
            )
            for row in phases
        ],
    )
    (run_dir / "loads.json").write_text(
        json.dumps([row.to_row() for row in loads], indent=2) + "\n", encoding="utf-8"
    )

    windows, capture_path = _windows_from_logs(
        cfg,
        run_dir,
        offsets=offsets,
        t0=t0,
        base_ts=started_wall,
        phases=phases,
        window_s=float(cfg.get_int("analysis.window_s", 1)),
        plain=plain_run,
    )
    evaluation = None
    if windows and evaluate_after:
        write_features(run_dir / "features.csv", windows)
        try:
            evaluation = evaluate(
                windows,
                folds=folds or int(cfg.get_int("analysis.cv_folds", 5)),
                seed=seed,
                bootstrap=int(cfg.get_int("analysis.bootstrap", 1000)),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, not fatal
            (run_dir / "evaluation-error.txt").write_text(str(exc), encoding="utf-8")

    slot_timing = None if plain_run else _slot_timing(cfg, offsets)
    result = RunResult(
        run_id=run_id,
        configuration=letter,
        persona=persona_id,
        loads=loads,
        summary=summarise(loads),
        windows=len(windows),
        active_windows=sum(1 for w in windows if w.label == "active"),
        idle_windows=sum(1 for w in windows if w.label == "idle"),
        evaluation=evaluation,
        duration_s=duration,
        slot_timing=slot_timing,
        driver=driver_note,
        artifacts={
            "dir": str(run_dir),
            "ground_truth": str(run_dir / "ground-truth.csv"),
            "loads": str(run_dir / "loads.json"),
            **({"capture": str(capture_path)} if capture_path else {}),
        },
    )
    result.write(run_dir / "summary.json")

    connection = open_db(Path(cfg.path("paths.sqlite")))
    total_window_s = sum(max(0.0, w.end - w.start) for w in windows)
    idle_window_s = sum(max(0.0, w.end - w.start) for w in windows if w.label == "idle")
    idle_window_bytes = sum(w.bytes for w in windows if w.label == "idle")
    window_bytes = sum(w.bytes for w in windows)
    try:
        record_run(
            connection,
            RunRecord(
                run_id=run_id,
                started_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
                configuration=letter,
                persona_id=persona_id,
                duration_s=duration,
                real_bytes=int(result.summary["bytes"]),
                cover_bytes=int(sum(w.bytes for w in windows)),
                # Wire bytes per byte of page content: config A approaches 1,
                # cover configurations pay for the padding records.
                overhead=round(
                    float(window_bytes) / max(1.0, float(result.summary["bytes"])),
                    3,
                ),
                wire_bps=round(float(window_bytes) / total_window_s, 1) if total_window_s else 0.0,
                idle_bps=(
                    round(float(idle_window_bytes) / idle_window_s, 1) if idle_window_s else 0.0
                ),
                notes=f"{CONFIGURATION_NOTES[letter]}; {driver_note}" if driver_note else CONFIGURATION_NOTES[letter],
            ),
            page_loads=[
                {
                    "index": row.index,
                    "site": row.site,
                    "status": row.status,
                    "bytes": row.bytes,
                    "load_ms": row.ms,
                    "error": row.error,
                }
                for row in loads
            ],
            scores=evaluation.to_dict() if evaluation else None,
            slot_timing=slot_timing,
        )
    finally:
        connection.close()
    return result


def _slot_timing(cfg: Config, offsets: dict[str, int]) -> dict[str, float] | None:
    """Slot timing error summary for the run, from the client slot log."""
    from cover.scheduler import SlotRecorder
    from experiments.capture import read_slot_log

    records, _ = read_slot_log(
        cfg.path("paths.run") / "logs" / "slots-client.jsonl",
        offset=offsets.get("client", 0),
    )
    if not records:
        return None
    recorder = SlotRecorder()
    for record in records:
        recorder.errors_ms.append(float(record.error_ms))
        recorder.sent += 1
    summary = dict(recorder.summary())
    return {
        "p50_error_ms": float(summary["p50_error_ms"]),
        "p95_error_ms": float(summary["p95_error_ms"]),
        "max_error_ms": float(summary["max_error_ms"]),
    }
