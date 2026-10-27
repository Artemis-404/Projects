"""Read-only view model for the dashboard (FR-25, SDD 7).

Everything the dashboard shows comes from files the engine and the experiment
runner already write: ``uqab.sqlite``, ``run/stats.json``, ``run/lab.json`` and the
persona/profile files.  The dashboard never writes to the run directory and never
touches the control API, so it is always safe to open while UQAB is running.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Config
from storage.db import comparison, open_db, page_loads, runs, scores


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(slots=True)
class DashboardModel:
    """A snapshot of everything the dashboard renders."""

    comparison: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    personas: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    lab: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    db_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparison": self.comparison,
            "runs": self.runs,
            "personas": self.personas,
            "stats": self.stats,
            "lab": self.lab,
            "profile": self.profile,
            "db_error": self.db_error,
        }

    @property
    def running(self) -> bool:
        return bool(self.lab.get("ready")) or bool(self.stats.get("session_started"))


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in dict(row)}


def load_personas(path: Path) -> list[dict[str, Any]]:
    """Persona summaries: id, source, description, validation state."""
    found: list[dict[str, Any]] = []
    if not path.is_dir():
        return found
    for candidate in sorted(path.glob("*.json")) + sorted((path / "generated").glob("*.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        states = data.get("states") or []
        found.append(
            {
                "id": str(data.get("id", candidate.stem)),
                "source": str(data.get("source", "")),
                "description": str(data.get("description", "")),
                "states": len(states) if isinstance(states, list) else 0,
                "floor_slot_ms": data.get("floor_slot_ms", 0),
                "generated": candidate.parent.name == "generated",
            }
        )
    return found


def load_model(cfg: Config, *, runs_limit: int = 25) -> DashboardModel:
    """Collect the dashboard snapshot, tolerating a not-yet-created database."""
    model = DashboardModel()
    model.stats = _read_json(cfg.path("paths.run") / "stats.json") or {}
    model.lab = _read_json(cfg.path("paths.run") / "lab.json") or {}
    model.profile = _read_json(Path(cfg.path("profile.path"))) or {}
    model.personas = load_personas(Path(cfg.path("persona.profiles_dir")))

    db_path = Path(cfg.path("paths.sqlite"))
    if not db_path.is_file():
        model.db_error = "no experiments recorded yet"
        return model
    try:
        connection = open_db(db_path)
    except sqlite3.Error as exc:  # pragma: no cover - unreadable database
        model.db_error = f"cannot open the database: {exc}"
        return model
    try:
        model.comparison = [summary.to_dict() for summary in comparison(connection)]
        model.runs = [_row_to_dict(row) for row in runs(connection)][:runs_limit]
    except sqlite3.Error as exc:  # pragma: no cover - partial schema
        model.db_error = f"database error: {exc}"
    finally:
        connection.close()
    return model


def run_detail(cfg: Config, run_id: str) -> dict[str, Any] | None:
    """One run with its page loads, classifier scores and slot timing."""
    db_path = Path(cfg.path("paths.sqlite"))
    if not db_path.is_file():
        return None
    connection = open_db(db_path)
    try:
        rows = [row for row in runs(connection) if row["run_id"] == run_id]
        if not rows:
            return None
        detail = _row_to_dict(rows[0])
        detail["page_loads"] = [_row_to_dict(row) for row in page_loads(connection, run_id)]
        detail["scores"] = [_row_to_dict(row) for row in scores(connection, run_id)]
        timing = connection.execute(
            "SELECT * FROM slot_timing WHERE run_id = ?", (run_id,)
        ).fetchone()
        detail["slot_timing"] = _row_to_dict(timing) if timing else None
        return detail
    finally:
        connection.close()


def summary_text(model: DashboardModel) -> str:
    """One-paragraph plain-English state of the project, used on the report page."""
    if not model.comparison:
        return (
            "No experiment has been recorded yet. Run `python -m experiments.compare` "
            "to produce the A/B/C table."
        )
    parts: list[str] = []
    for row in model.comparison:
        parts.append(
            f"Configuration {row['configuration']} reached F1 {row['f1']:.3f} "
            f"(95% CI {row['f1_ci_low']:.3f}-{row['f1_ci_high']:.3f}) at a median "
            f"page load of {row['p50_ms']:.0f} ms with overhead {row['overhead']:.2f}x"
        )
    return "; ".join(parts) + "."
