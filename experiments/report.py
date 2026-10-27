"""Markdown report generator (FR-25, phase plan M7).

Turns whatever is in the run database into the tables the written report needs:

* the A/B/C comparison (observer F1 with 95% CI, median page-load latency,
  overhead),
* per-configuration run details (personas, durations, byte counts, slot timing),
* a short interpretation that states the result honestly — including when the
  data does not support a conclusion.

Usage::

    python -m experiments.report --out docs/report/results.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Config, load_config
from storage.db import comparison, open_db, runs

CONFIG_DESCRIPTIONS = {
    "A": "plain Tor (no cover)",
    "B": "constant-rate cover (static `constant` persona)",
    "C": "persona cover (timetable driven)",
}


@dataclass(slots=True)
class ReportData:
    generated_at: str
    configurations: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    statistics: dict[str, Any]
    notes: list[str]


def _rows(connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [{key: row[key] for key in dict(row)} for row in connection.execute(query, params).fetchall()]


def collect(cfg: Config) -> ReportData:
    """Read the database into the structures the report renders."""
    db_path = Path(cfg.path("paths.sqlite"))
    notes: list[str] = []
    if not db_path.is_file():
        return ReportData(
            generated_at=time.strftime("%Y-%m-%d %H:%M"),
            configurations=[],
            runs=[],
            statistics={},
            notes=[f"no database at {db_path}: run `python -m experiments.compare` first"],
        )
    connection = open_db(db_path)
    try:
        summaries = [summary.to_dict() for summary in comparison(connection)]
        run_rows = []
        for row in runs(connection):
            scores_rows = _rows(
                connection, "SELECT * FROM scores WHERE run_id = ?", (row["run_id"],)
            )
            timing = _rows(
                connection, "SELECT * FROM slot_timing WHERE run_id = ?", (row["run_id"],)
            )
            run_rows.append(
                {
                    **{key: row[key] for key in dict(row)},
                    "scores": scores_rows,
                    "slot_timing": timing[0] if timing else None,
                }
            )
        load_stats = _rows(
            connection,
            """
            SELECT run_id, COUNT(*) AS loads,
                   SUM(CASE WHEN status BETWEEN 200 AND 399 THEN 1 ELSE 0 END) AS ok,
                   AVG(load_ms) AS mean_ms
            FROM page_loads GROUP BY run_id
            """,
        )
    finally:
        connection.close()
    if not summaries:
        notes.append("the database has no scored runs yet")
    statistics = {
        "runs": len(run_rows),
        "configurations": len(summaries),
        "page_loads": sum(int(row["loads"] or 0) for row in load_stats),
        "loads_ok": sum(int(row["ok"] or 0) for row in load_stats),
    }
    return ReportData(
        generated_at=time.strftime("%Y-%m-%d %H:%M"),
        configurations=summaries,
        runs=run_rows,
        statistics=statistics,
        notes=notes,
    )


def _fmt(value: Any, digits: int = 3) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def comparison_table(data: ReportData) -> str:
    """The headline table: privacy (F1) against cost (latency, overhead)."""
    header = (
        "| Config | Meaning | Runs | Loads | Median load (ms) | Overhead | "
        "Accuracy | F1 | 95% CI |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    lines = [header]
    for row in data.configurations:
        lines.append(
            f"| {row['configuration']} | {CONFIG_DESCRIPTIONS.get(str(row['configuration']), '')} "
            f"| {row['runs']} | {row['loads']} | {_fmt(row['p50_ms'], 0)} | "
            f"{_fmt(row['overhead'])}x | {_fmt(row['accuracy'])} | {_fmt(row['f1'])} | "
            f"{_fmt(row['f1_ci_low'])}–{_fmt(row['f1_ci_high'])} |"
        )
    if len(lines) == 1:
        lines.append("| - | no runs recorded | - | - | - | - | - | - | - |")
    return "\n".join(lines)


def runs_table(data: ReportData) -> str:
    header = (
        "| Run | Config | Persona | Started | Duration (s) | Real bytes | Cover bytes | "
        "Overhead | p95 slot error (ms) |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    lines = [header]
    for run in data.runs:
        timing = run.get("slot_timing") or {}
        lines.append(
            f"| {run['run_id']} | {run['configuration']} | {run['persona_id']} | "
            f"{run['started_at']} | {_fmt(run['duration_s'], 1)} | {int(run['real_bytes'])} | "
            f"{int(run['cover_bytes'])} | {_fmt(run['overhead'])}x | "
            f"{_fmt(timing.get('p95_error_ms'), 2)} |"
        )
    if len(lines) == 1:
        lines.append("| - | - | - | - | - | - | - | - | - |")
    return "\n".join(lines)


def classifier_table(data: ReportData) -> str:
    header = (
        "| Run | Classifier | Folds | Accuracy | Precision | Recall | F1 | 95% CI |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    lines = [header]
    for run in data.runs:
        for score in run.get("scores") or []:
            lines.append(
                f"| {run['run_id']} | {score['classifier']} | {score['cv_folds']} | "
                f"{_fmt(score['accuracy'])} | {_fmt(score['precision'])} | "
                f"{_fmt(score['recall'])} | {_fmt(score['f1'])} | "
                f"{_fmt(score['f1_ci_low'])}–{_fmt(score['f1_ci_high'])} |"
            )
    if len(lines) == 1:
        lines.append("| - | - | - | - | - | - | - | - |")
    return "\n".join(lines)


def interpretation(data: ReportData) -> str:
    """State the finding, or say plainly that there is not enough data."""
    by_config = {str(row["configuration"]): row for row in data.configurations}
    if not {"A", "C"} <= set(by_config):
        return (
            "The database does not yet contain both configuration A (plain Tor) and "
            "configuration C (persona cover), so the research question cannot be "
            "answered from these numbers. Run `python -m experiments.compare`."
        )
    plain = by_config["A"]
    persona = by_config["C"]
    delta_f1 = float(plain["f1"]) - float(persona["f1"])
    latency_ratio = (
        float(persona["p50_ms"]) / float(plain["p50_ms"]) if float(plain["p50_ms"]) else 0.0
    )
    parts = [
        f"Plain Tor was classified with F1 {_fmt(plain['f1'])} "
        f"(95% CI {_fmt(plain['f1_ci_low'])}–{_fmt(plain['f1_ci_high'])}); "
        f"persona cover with F1 {_fmt(persona['f1'])} "
        f"(95% CI {_fmt(persona['f1_ci_low'])}–{_fmt(persona['f1_ci_high'])})."
    ]
    if delta_f1 > 0.05:
        parts.append(
            f"Persona cover reduced the observer's ACTIVE/IDLE F1 by {_fmt(delta_f1)} "
            "in this dataset, i.e. it made the inference task harder."
        )
    elif delta_f1 < -0.05:
        parts.append(
            f"Persona cover *increased* F1 by {_fmt(-delta_f1)} here: the persona's own "
            "state changes were themselves informative in this dataset."
        )
    else:
        parts.append(
            "The difference in F1 is smaller than 0.05 and inside the reported intervals, "
            "so this dataset does not support a claim either way."
        )
    if latency_ratio:
        parts.append(
            f"Cost: the median page load was {_fmt(latency_ratio, 2)}x plain Tor and the "
            f"cover overhead was {_fmt(persona['overhead'])}x the page bytes."
        )
    if "B" in by_config:
        constant = by_config["B"]
        parts.append(
            f"Constant-rate cover (B) scored F1 {_fmt(constant['f1'])} at "
            f"{_fmt(constant['overhead'])}x overhead; it is the baseline that persona "
            "cover has to beat on cost without giving up inference resistance."
        )
    return " ".join(parts)


def render(data: ReportData, *, title: str = "UQAB evaluation results") -> str:
    """The markdown document."""
    notes = "\n".join(f"- {note}" for note in data.notes) or "- none"
    return f"""# {title}

Generated {data.generated_at} by `python -m experiments.report` from the local
`uqab.sqlite` database. Every number below comes from a run recorded by
`python -m experiments.compare`; nothing is typed in by hand.

## Headline comparison

{comparison_table(data)}

Lower F1 means the observer had more trouble telling ACTIVE from IDLE browsing.
The 95% interval is a percentile bootstrap over the pooled cross-validation
predictions (NFR-05).

## Interpretation

{interpretation(data)}

## Runs

{runs_table(data)}

## Classifier scores

{classifier_table(data)}

## Totals

| metric | value |
| --- | --- |
| runs recorded | {data.statistics.get('runs', 0)} |
| configurations | {data.statistics.get('configurations', 0)} |
| page loads attempted | {data.statistics.get('page_loads', 0)} |
| page loads succeeded | {data.statistics.get('loads_ok', 0)} |

## Notes and caveats

{notes}

- Laboratory runs (no Tor binary) exercise the whole pipeline but not Tor's
  circuits; quote them as pipeline results, and quote real-bridge runs for claims
  about the Tor network. See `docs/LAB_MODE.md`.
- Configuration A in lab mode relays straight to the demo origin (see D7 in
  `docs/DESIGN_DECISIONS.md`).
- Personas, windows and seeds are recorded per run, so every row can be replayed.
"""


def write_report(cfg: Config, out: Path | str, *, title: str = "UQAB evaluation results") -> Path:
    data = collect(cfg)
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(data, title=title), encoding="utf-8")
    payload = target.with_suffix(".json")
    payload.write_text(
        json.dumps(
            {
                "generated_at": data.generated_at,
                "configurations": data.configurations,
                "statistics": data.statistics,
                "notes": data.notes,
                "interpretation": interpretation(data),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the UQAB results report as markdown")
    parser.add_argument("--config", default=None)
    parser.add_argument("--out", default="docs/report/results.md")
    parser.add_argument("--title", default="UQAB evaluation results")
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config)) if args.config else load_config()
    target = write_report(cfg, args.out, title=args.title)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
