"""A/B/C comparison: run every configuration and export the results (FR-24, FR-25).

The research question the project answers needs three numbers side by side:

* inference difficulty (ACTIVE/IDLE F1 of the observers),
* cost (page-load latency against plain Tor),
* cover overhead (wire bytes per byte of page content).

``compare`` runs the three configurations on the same timeline and writes
``comparison.json`` plus ``comparison.csv`` next to the run folders, and stores
one row per configuration in SQLite so the dashboard can chart them.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Config, load_config
from experiments.run import (
    CONFIG_PERSONAS,
    RunResult,
    run_experiment,
    timeline_from_config,
)
from experiments.runner import Timeline
from storage.db import RunSummary, comparison, db_session, export_comparison


@dataclass(slots=True)
class Comparison:
    """The three-run comparison produced by ``compare``."""

    results: list[RunResult] = field(default_factory=list)
    summaries: list[RunSummary] = field(default_factory=list)
    started_at: str = ""
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 3),
            "notes": self.notes,
            "runs": [result.to_dict() for result in self.results],
            "summary": [summary.to_dict() for summary in self.summaries],
        }

    @property
    def ok(self) -> bool:
        return all(not result.error for result in self.results)

    def write(self, out_dir: Path | str) -> dict[str, Path]:
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        json_path = target / "comparison.json"
        json_path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        csv_path = target / "comparison.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "configuration",
                    "persona",
                    "runs",
                    "loads",
                    "p50_ms",
                    "overhead",
                    "wire_bps",
                    "idle_bps",
                    "accuracy",
                    "f1",
                    "f1_ci_low",
                    "f1_ci_high",
                ]
            )
            by_config = {result.configuration: result for result in self.results}
            for summary in self.summaries:
                row = summary.to_dict()
                persona = by_config.get(summary.configuration)
                writer.writerow(
                    [
                        summary.configuration,
                        persona.persona if persona else "",
                        row["runs"],
                        row["loads"],
                        row["p50_ms"],
                        row["overhead"],
                        row["wire_bps"],
                        row["idle_bps"],
                        row["accuracy"],
                        row["f1"],
                        row["f1_ci_low"],
                        row["f1_ci_high"],
                    ]
                )
        return {"json": json_path, "csv": csv_path}


def run_comparison(
    *,
    cfg: Config | None = None,
    configurations: tuple[str, ...] = ("A", "B", "C"),
    timeline: Timeline | None = None,
    loads: int | None = None,
    out_dir: Path | str | None = None,
    use_lab: bool = True,
    repeats: int = 1,
) -> Comparison:
    """Run each configuration ``repeats`` times on the same timeline.

    Repeats differ in the RF/classifier seed and in the phase jitter drawn by the
    replay, so the reported mean/CI describe run-to-run variance rather than one
    lucky split.
    """
    cfg = cfg or load_config()
    target = Path(out_dir) if out_dir is not None else Path(cfg.path("paths.experiments"))
    timeline = timeline or timeline_from_config(cfg, loads=loads)

    started = time.time()
    comparison_result = Comparison(started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    base_seed = int(cfg.get_int("analysis.random_seed", 7))
    for configuration in configurations:
        persona = CONFIG_PERSONAS.get(configuration.upper())
        for repeat in range(max(1, int(repeats))):
            result = run_experiment(
                configuration,
                cfg=cfg,
                timeline=timeline,
                persona=persona,
                use_lab=use_lab,
                out_dir=target,
                seed=base_seed + repeat,
            )
            comparison_result.results.append(result)
            if result.error:
                comparison_result.notes.append(f"{configuration}: {result.error}")
            if result.active_windows == 0 or result.idle_windows == 0:
                comparison_result.notes.append(
                    f"{configuration}: no ACTIVE/IDLE split, so no F1 is reported"
                )

    with db_session(Path(cfg.path("paths.sqlite"))) as connection:
        comparison_result.summaries = comparison(connection)
        export_comparison(connection, target / "comparison-db.json")

    comparison_result.duration_s = time.time() - started
    written = comparison_result.write(target)
    comparison_result.notes.append(f"wrote {written['json'].name} and {written['csv'].name}")
    return comparison_result


def format_table(summaries: list[RunSummary]) -> str:
    """Plain-text table for the console and the report appendix."""
    header = (
        f"{'cfg':>3}  {'runs':>4}  {'loads':>5}  {'p50 ms':>7}  "
        f"{'overhead':>8}  {'kbit/s':>7}  {'idle B/s':>8}  {'accuracy':>8}  {'F1':>6}  {'95% CI':>13}"
    )
    lines = [header, "-" * len(header)]
    for summary in summaries:
        row = summary.to_dict()
        lines.append(
            f"{row['configuration']:>3}  {row['runs']:>4}  {row['loads']:>5}  "
            f"{row['p50_ms']:>7.1f}  {row['overhead']:>8.2f}  "
            f"{row['wire_bps'] * 8 / 1000:>7.1f}  {row['idle_bps']:>8.1f}  {row['accuracy']:>8.3f}  "
            f"{row['f1']:>6.3f}  "
            f"[{row['f1_ci_low']:.3f}, {row['f1_ci_high']:.3f}]"
        )
    if len(lines) == 2:
        lines.append("(no runs yet - run `python -m experiments.compare` first)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare configurations A (plain), B (constant) and C (persona)"
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--loads", type=int, default=None, help="page loads per configuration")
    parser.add_argument("--out", default=None, help="output directory")
    parser.add_argument(
        "--configurations",
        default="A,B,C",
        help="comma separated subset, e.g. --configurations C",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="run each configuration N times and aggregate (default 1)",
    )
    parser.add_argument(
        "--no-lab",
        action="store_true",
        help="use a real Tor SOCKS port instead of the lab stack",
    )
    parser.add_argument("--json", action="store_true", help="print the full JSON")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config)) if args.config else load_config()
    names = tuple(part.strip().upper() for part in args.configurations.split(",") if part.strip())
    result = run_comparison(
        cfg=cfg,
        configurations=names,
        loads=args.loads,
        out_dir=args.out,
        use_lab=not args.no_lab,
        repeats=max(1, args.repeats),
    )
    print(format_table(result.summaries), flush=True)
    for note in result.notes:
        print(f"note: {note}")
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
