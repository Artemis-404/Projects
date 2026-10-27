"""SQLite storage for runs, page loads, classifier scores and slot timing (SDD 6.3).

The schema is fixed by the SDD so the dashboard, the report and the comparison
script all read the same tables.  Connections are short-lived and per-call;
WAL is enabled so the dashboard can read while an experiment writes.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    configuration TEXT NOT NULL,
    persona_id   TEXT NOT NULL,
    git_tag      TEXT,
    duration_s   REAL NOT NULL DEFAULT 0,
    real_bytes   INTEGER NOT NULL DEFAULT 0,
    cover_bytes  INTEGER NOT NULL DEFAULT 0,
    overhead     REAL NOT NULL DEFAULT 0,
    wire_bps     REAL NOT NULL DEFAULT 0,
    idle_bps     REAL NOT NULL DEFAULT 0,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS page_loads (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    site      TEXT,
    status    INTEGER NOT NULL DEFAULT 0,
    bytes     INTEGER NOT NULL DEFAULT 0,
    load_ms   REAL NOT NULL DEFAULT 0,
    error     TEXT
);

CREATE TABLE IF NOT EXISTS scores (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    classifier TEXT NOT NULL,
    cv_folds   INTEGER NOT NULL DEFAULT 5,
    accuracy   REAL NOT NULL DEFAULT 0,
    precision  REAL NOT NULL DEFAULT 0,
    recall     REAL NOT NULL DEFAULT 0,
    f1         REAL NOT NULL DEFAULT 0,
    f1_ci_low  REAL NOT NULL DEFAULT 0,
    f1_ci_high REAL NOT NULL DEFAULT 0,
    UNIQUE(run_id, classifier)
);

CREATE TABLE IF NOT EXISTS slot_timing (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    p50_error_ms  REAL NOT NULL DEFAULT 0,
    p95_error_ms  REAL NOT NULL DEFAULT 0,
    max_error_ms  REAL NOT NULL DEFAULT 0,
    UNIQUE(run_id)
);

CREATE INDEX IF NOT EXISTS idx_runs_configuration ON runs(configuration);
CREATE INDEX IF NOT EXISTS idx_page_loads_run ON page_loads(run_id);
CREATE INDEX IF NOT EXISTS idx_scores_run ON scores(run_id);
"""


@dataclass(slots=True)
class RunRecord:
    run_id: str
    started_at: str
    configuration: str
    persona_id: str
    duration_s: float = 0.0
    real_bytes: int = 0
    cover_bytes: int = 0
    overhead: float = 0.0
    #: Wire bytes per second over the whole run (same path as ``windows``).
    wire_bps: float = 0.0
    #: Wire bytes per second over windows labelled IDLE.  Zero means the observer
    #: sees silence whenever the user is not browsing, which is precisely the leak
    #: persona cover is supposed to remove (D21).
    idle_bps: float = 0.0
    notes: str = ""
    git_tag: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "configuration": self.configuration,
            "persona_id": self.persona_id,
            "git_tag": self.git_tag,
            "duration_s": float(self.duration_s),
            "real_bytes": int(self.real_bytes),
            "cover_bytes": int(self.cover_bytes),
            "overhead": float(self.overhead),
            "wire_bps": float(self.wire_bps),
            "idle_bps": float(self.idle_bps),
            "notes": self.notes,
        }


@dataclass(slots=True)
class RunSummary:
    """One row of the comparison table."""

    configuration: str
    runs: int = 0
    loads: int = 0
    p50_ms: float = 0.0
    overhead: float = 0.0
    wire_bps: float = 0.0
    idle_bps: float = 0.0
    accuracy: float = 0.0
    f1: float = 0.0
    f1_ci_low: float = 0.0
    f1_ci_high: float = 0.0
    classifiers: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "configuration": self.configuration,
            "runs": self.runs,
            "loads": self.loads,
            "p50_ms": round(self.p50_ms, 3),
            "overhead": round(self.overhead, 4),
            "wire_bps": round(self.wire_bps, 1),
            "idle_bps": round(self.idle_bps, 1),
            "accuracy": round(self.accuracy, 4),
            "f1": round(self.f1, 4),
            "f1_ci_low": round(self.f1_ci_low, 4),
            "f1_ci_high": round(self.f1_ci_high, 4),
        }


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open (and create) the database, applying the schema idempotently."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA)
    _migrate(connection)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return connection


#: Columns added after the first release; an existing database gains them in place.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("wire_bps", "REAL NOT NULL DEFAULT 0"),
    ("idle_bps", "REAL NOT NULL DEFAULT 0"),
)


def _migrate(connection: sqlite3.Connection) -> None:
    existing = {row[1] for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
    for name, definition in _ADDED_COLUMNS:
        if name not in existing:
            connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")


@contextmanager
def db_session(path: Path | str) -> Iterator[sqlite3.Connection]:
    connection = open_db(path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def git_tag() -> str:
    """Short commit hash of the checkout, or an empty string outside git."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def record_run(
    connection: sqlite3.Connection,
    run: RunRecord,
    *,
    page_loads: Iterable[dict[str, Any]] = (),
    scores: dict[str, Any] | None = None,
    slot_timing: dict[str, Any] | None = None,
) -> None:
    """Insert (or replace) a run plus its loads, scores and slot timing."""
    if not run.git_tag:
        run.git_tag = git_tag()
    connection.execute(
        """
        INSERT INTO runs (run_id, started_at, configuration, persona_id, git_tag,
                          duration_s, real_bytes, cover_bytes, overhead, wire_bps,
                          idle_bps, notes)
        VALUES (:run_id, :started_at, :configuration, :persona_id, :git_tag,
                :duration_s, :real_bytes, :cover_bytes, :overhead, :wire_bps,
                :idle_bps, :notes)
        ON CONFLICT(run_id) DO UPDATE SET
            started_at=excluded.started_at,
            configuration=excluded.configuration,
            persona_id=excluded.persona_id,
            git_tag=excluded.git_tag,
            duration_s=excluded.duration_s,
            real_bytes=excluded.real_bytes,
            cover_bytes=excluded.cover_bytes,
            overhead=excluded.overhead,
            wire_bps=excluded.wire_bps,
            idle_bps=excluded.idle_bps,
            notes=excluded.notes
        """,
        run.to_row(),
    )
    connection.execute("DELETE FROM page_loads WHERE run_id = ?", (run.run_id,))
    for row in page_loads:
        connection.execute(
            """
            INSERT INTO page_loads (run_id, idx, site, status, bytes, load_ms, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                int(row.get("index", 0)),
                str(row.get("site", "")),
                int(row.get("status", 0) or 0),
                int(row.get("bytes", 0) or 0),
                float(row.get("load_ms", 0.0) or 0.0),
                str(row.get("error", "") or ""),
            ),
        )
    if scores:
        _record_scores(connection, run.run_id, scores)
    if slot_timing:
        record_slot_timing(connection, run.run_id, slot_timing)
    connection.commit()


def _record_scores(connection: sqlite3.Connection, run_id: str, scores: dict[str, Any]) -> None:
    folds = int(scores.get("folds", 5) or 5)
    models: dict[str, dict[str, Any]] = {}
    if isinstance(scores.get("lr"), dict):
        models["logreg"] = scores["lr"]
    if isinstance(scores.get("rf"), dict):
        models["random_forest"] = scores["rf"]
    if not models and "accuracy" in scores:
        models["unknown"] = scores
    for name, metrics in models.items():
        connection.execute(
            """
            INSERT INTO scores (run_id, classifier, cv_folds, accuracy, precision,
                                recall, f1, f1_ci_low, f1_ci_high)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, classifier) DO UPDATE SET
                cv_folds=excluded.cv_folds,
                accuracy=excluded.accuracy,
                precision=excluded.precision,
                recall=excluded.recall,
                f1=excluded.f1,
                f1_ci_low=excluded.f1_ci_low,
                f1_ci_high=excluded.f1_ci_high
            """,
            (
                run_id,
                name,
                folds,
                float(metrics.get("accuracy", 0.0)),
                float(metrics.get("precision", 0.0)),
                float(metrics.get("recall", 0.0)),
                float(metrics.get("f1", 0.0)),
                float(metrics.get("f1_ci_low", 0.0)),
                float(metrics.get("f1_ci_high", 0.0)),
            ),
        )


def record_slot_timing(
    connection: sqlite3.Connection, run_id: str, timing: dict[str, Any]
) -> None:
    connection.execute(
        """
        INSERT INTO slot_timing (run_id, p50_error_ms, p95_error_ms, max_error_ms)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            p50_error_ms=excluded.p50_error_ms,
            p95_error_ms=excluded.p95_error_ms,
            max_error_ms=excluded.max_error_ms
        """,
        (
            run_id,
            float(timing.get("p50_error_ms", 0.0) or 0.0),
            float(timing.get("p95_error_ms", 0.0) or 0.0),
            float(timing.get("max_error_ms", 0.0) or 0.0),
        ),
    )
    connection.commit()


def runs(connection: sqlite3.Connection, configuration: str | None = None) -> list[sqlite3.Row]:
    if configuration is None:
        cursor = connection.execute("SELECT * FROM runs ORDER BY started_at DESC")
    else:
        cursor = connection.execute(
            "SELECT * FROM runs WHERE configuration = ? ORDER BY started_at DESC",
            (configuration.upper(),),
        )
    return list(cursor.fetchall())


def latest_runs(connection: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
    )


def page_loads(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM page_loads WHERE run_id = ? ORDER BY idx", (run_id,)
        ).fetchall()
    )


def scores(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM scores WHERE run_id = ? ORDER BY classifier", (run_id,)
        ).fetchall()
    )


def comparison(connection: sqlite3.Connection) -> list[RunSummary]:
    """Aggregate every configuration: page-load latency, overhead, F1 with CI."""
    summaries: list[RunSummary] = []
    configs = [row[0] for row in connection.execute(
        "SELECT DISTINCT configuration FROM runs ORDER BY configuration"
    ).fetchall()]
    for config in configs:
        run_rows = runs(connection, config)
        if not run_rows:
            continue
        run_ids = [row["run_id"] for row in run_rows]
        placeholders = ",".join("?" for _ in run_ids)
        latencies = [
            float(row[0])
            for row in connection.execute(
                f"SELECT load_ms FROM page_loads WHERE run_id IN ({placeholders}) "
                "AND status BETWEEN 200 AND 399 ORDER BY load_ms",
                run_ids,
            ).fetchall()
        ]
        score_rows = connection.execute(
            f"SELECT * FROM scores WHERE run_id IN ({placeholders})",
            run_ids,
        ).fetchall()
        overheads = [float(row["overhead"] or 0.0) for row in run_rows if row["overhead"]]
        wire_rates = [float(row["wire_bps"] or 0.0) for row in run_rows]
        idle_rates = [float(row["idle_bps"] or 0.0) for row in run_rows]
        summary = RunSummary(
            configuration=config,
            runs=len(run_rows),
            loads=len(latencies),
            p50_ms=latencies[len(latencies) // 2] if latencies else 0.0,
            overhead=(sum(overheads) / len(overheads)) if overheads else 0.0,
            wire_bps=(sum(wire_rates) / len(wire_rates)) if wire_rates else 0.0,
            idle_bps=(sum(idle_rates) / len(idle_rates)) if idle_rates else 0.0,
        )
        if score_rows:
            summary.accuracy = sum(r["accuracy"] for r in score_rows) / len(score_rows)
            summary.f1 = sum(r["f1"] for r in score_rows) / len(score_rows)
            summary.f1_ci_low = min(r["f1_ci_low"] for r in score_rows)
            summary.f1_ci_high = max(r["f1_ci_high"] for r in score_rows)
            summary.classifiers = len(score_rows)
        summaries.append(summary)
    return summaries


def export_comparison(connection: sqlite3.Connection, path: Path | str) -> Path:
    """Write the A/B/C comparison table as JSON for the report and dashboard."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_by": "uqab.evaluation",
        "configurations": [summary.to_dict() for summary in comparison(connection)],
    }
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def csv_rows(summaries: Sequence[RunSummary]) -> list[list[str]]:
    """CSV-ready rows (header first) for the report appendix."""
    header = [
        "configuration",
        "runs",
        "loads",
        "p50_ms",
        "overhead",
        "wire_bps",
        "idle_bps",
        "accuracy",
        "f1",
        "ci_low",
        "ci_high",
    ]
    rows = [header]
    for summary in summaries:
        data = summary.to_dict()
        rows.append([str(data[key]) for key in header])
    return rows


@dataclass(slots=True)
class ComparisonStats:
    """Plain-data view used by the dashboard without a live connection."""

    configurations: list[RunSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"configurations": [item.to_dict() for item in self.configurations]}
