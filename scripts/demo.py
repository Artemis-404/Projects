"""One-command demonstration: lab session, A/B/C comparison, report, dashboard.

This is the "show it to the examiner" script. It needs no Tor, no browser and no
network beyond loopback, and it finishes in about a minute:

1. generate the lab certificates if they are missing;
2. replay a short scripted browsing timeline through the real transport for
   configurations A (plain), B (constant cover) and C (persona cover);
3. train the ACTIVE/IDLE observers and store everything in SQLite;
4. render the markdown/JSON report and render the dashboard page once (as a smoke
   test that the HTML builds) without leaving a server running;
5. print the comparison table and where the artifacts are.

Usage::

    python scripts/demo.py                    # 4 page loads per configuration
    python scripts/demo.py --loads 10         # a longer run
    python scripts/demo.py --no-dashboard     # skip the dashboard check
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config, load_config  # noqa: E402
from experiments.compare import format_table, run_comparison  # noqa: E402
from experiments.report import write_report  # noqa: E402
from tunnel.certs import CertPaths, ensure  # noqa: E402


def demo_config(base: Config, run_dir: Path) -> Config:
    """A configuration that writes only inside the demo directory."""
    return base.with_overrides(
        **{
            "paths.run": str(run_dir / "run"),
            "paths.data": str(run_dir / "data"),
            "paths.sqlite": str(run_dir / "data" / "uqab.sqlite"),
            "paths.experiments": str(run_dir / "experiments"),
            "profile.path": str(run_dir / "data" / "profile" / "profile.json"),
            "profile.opt_in_flag": str(run_dir / "data" / "profile" / "opt_in.json"),
            "cover.retry_s": 0.5,
            "cover.stats_interval_s": 0.5,
            "pt.connect_timeout_s": 15,
        }
    )


def dashboard_smoke(cfg: Config) -> tuple[bool, str]:
    """Render the dashboard page over HTTP once, then shut the server down."""
    from dashboard.server import start_background

    server, thread = start_background(cfg, port=0)
    try:
        host = str(server.server_address[0])
        port = int(server.server_address[1])
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=10) as response:
            body = response.read().decode("utf-8")
        ok = response.status == 200 and "Configurations" in body
        return ok, f"{len(body)} bytes of HTML on http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the UQAB demo end to end")
    parser.add_argument("--loads", type=int, default=4, help="page loads per configuration")
    parser.add_argument("--idle", type=float, default=2.0, help="seconds of IDLE per load")
    parser.add_argument(
        "--repeats", type=int, default=1, help="runs per configuration (default 1)"
    )
    parser.add_argument("--keep", action="store_true", help="keep the demo directory")
    parser.add_argument("--out", default="docs/report/sample-results.md")
    parser.add_argument("--no-dashboard", action="store_true")
    args = parser.parse_args(argv)

    started = time.time()
    base = load_config()
    ensure(CertPaths.from_config(base))
    run_dir = Path(tempfile.mkdtemp(prefix="uqab-demo-"))
    cfg = demo_config(base, run_dir)

    from experiments.run import timeline_from_config

    timeline = timeline_from_config(cfg, loads=args.loads)
    # The demo keeps IDLE short so the whole thing finishes quickly.
    for phase in timeline.phases:
        if phase.kind == "idle":
            phase.duration_s = args.idle
    print(
        f"[demo] {args.loads} page loads per configuration, "
        f"{timeline.total_s:.0f}s per configuration, {args.repeats} run(s) each",
        flush=True,
    )

    result = run_comparison(
        cfg=cfg,
        configurations=("A", "B", "C"),
        timeline=timeline,
        repeats=max(1, args.repeats),
    )
    print()
    print(format_table(result.summaries))
    print()

    for run in result.results:
        state = "ok" if not run.error else f"error: {run.error}"
        print(
            f"[demo] {run.configuration}: {run.summary['ok']}/{run.summary['loads']} loads, "
            f"{run.windows} windows ({run.active_windows} active / {run.idle_windows} idle), "
            f"p50 {run.summary['p50_ms']:.0f} ms, {state}"
        )

    out = Path(args.out)
    report = write_report(cfg, out, title="UQAB laboratory demo results")
    print(f"[demo] report: {report}")
    print(f"[demo] json:   {report.with_suffix('.json')}")

    if not args.no_dashboard:
        ok, detail = dashboard_smoke(cfg)
        print(f"[demo] dashboard: {'ok' if ok else 'FAILED'} ({detail})")

    summary = {
        "loads_per_configuration": args.loads,
        "repeats": max(1, args.repeats),
        "duration_s": round(time.time() - started, 1),
        "configurations": [row.to_dict() for row in result.summaries],
        "notes": result.notes,
        "demo_dir": str(run_dir),
    }
    print(json.dumps(summary, indent=2))

    if not args.keep:
        shutil.rmtree(run_dir, ignore_errors=True)
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
