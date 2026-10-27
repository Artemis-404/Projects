"""HTML rendering for the dashboard (FR-25).

Plain string templates keep the dashboard dependency-free: it runs on the bundled
Windows runtime with nothing but the standard library.  Charts are inline SVG, so
they render in any browser and need no JavaScript framework.
"""

from __future__ import annotations

from html import escape
from typing import Any

from dashboard.data import DashboardModel, summary_text

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px; background: #0e1117; color: #e6edf3;
  font: 15px/1.55 "Segoe UI", system-ui, -apple-system, sans-serif;
}
h1, h2, h3 { font-weight: 600; margin: 0 0 12px; }
h1 { font-size: 26px; letter-spacing: .04em; }
h2 { font-size: 18px; margin-top: 32px; color: #9fb3c8; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.brand { display: flex; align-items: baseline; gap: 14px; margin-bottom: 4px; }
.brand span { color: #58a6ff; letter-spacing: .38em; font-size: 13px; text-transform: uppercase; }
.sub { color: #7d8794; margin: 0 0 24px; }
.grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
.card { background: #161b22; border: 1px solid #232a33; border-radius: 12px; padding: 16px 18px; }
.card .label { color: #7d8794; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }
.card .value { font-size: 26px; font-weight: 600; margin-top: 6px; }
.card .hint { color: #7d8794; font-size: 12px; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 12px; overflow: hidden; }
th, td { padding: 10px 14px; text-align: right; border-bottom: 1px solid #232a33; font-variant-numeric: tabular-nums; }
th:first-child, td:first-child { text-align: left; }
th { color: #7d8794; font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
tr:last-child td { border-bottom: none; }
.pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.ok { background: #12351f; color: #56d364; }
.warn { background: #3a2a10; color: #e3b341; }
.bad { background: #3c1618; color: #f85149; }
.muted { color: #7d8794; }
.bar { height: 8px; border-radius: 4px; background: #232a33; overflow: hidden; }
.bar span { display: block; height: 100%; background: linear-gradient(90deg, #1f6feb, #58a6ff); }
footer { margin-top: 40px; color: #58606b; font-size: 12px; }
code { background: #161b22; border: 1px solid #232a33; border-radius: 5px; padding: 1px 5px; }
"""


def page(title: str, body: str, *, refresh_s: int | None = None) -> str:
    """Wrap rendered sections in the shared shell."""
    refresh = f'<meta http-equiv="refresh" content="{refresh_s}">' if refresh_s else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} - UQAB</title>
{refresh}
<style>{CSS}</style>
</head><body>
<header>
  <div class="brand"><h1>UQAB</h1><span>evaluation dashboard</span></div>
  <p class="sub">Persona-driven cover traffic vs plain Tor and constant-rate cover.</p>
</header>
{body}
<footer>UQAB - BS Information Security Engineering, semester 5 project.
Reads the local run database only; it never opens the network.</footer>
</body></html>"""


def _num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def stat_card(label: str, value: str, hint: str = "") -> str:
    hint_html = f'<div class="hint">{escape(hint)}</div>' if hint else ""
    return (
        f'<div class="card"><div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(value)}</div>{hint_html}</div>'
    )


def f1_bar(value: float, low: float, high: float) -> str:
    width = max(0.0, min(1.0, float(value or 0.0))) * 100
    return (
        f'<div class="bar" title="F1 {_num(value, 3)} (CI {_num(low, 3)}-{_num(high, 3)})">'
        f'<span style="width:{width:.1f}%"></span></div>'
    )


def comparison_table(rows: list[dict[str, Any]]) -> str:
    """The A/B/C headline table: inference accuracy, cost and overhead."""
    if not rows:
        return '<p class="muted">No comparison data yet.</p>'
    head = (
        "<tr><th>Config</th><th>Runs</th><th>Loads</th><th>Median load</th>"
        "<th>Overhead</th><th>Accuracy</th><th>F1</th><th>95% CI</th><th></th></tr>"
    )
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td><strong>{escape(str(row.get('configuration', '?')))}</strong></td>"
            f"<td>{int(row.get('runs', 0) or 0)}</td>"
            f"<td>{int(row.get('loads', 0) or 0)}</td>"
            f"<td>{_num(row.get('p50_ms'), 0)} ms</td>"
            f"<td>{_num(row.get('overhead'))}x</td>"
            f"<td>{_num(row.get('accuracy'), 3)}</td>"
            f"<td>{_num(row.get('f1'), 3)}</td>"
            f"<td class=\"muted\">{_num(row.get('f1_ci_low'), 3)} - "
            f"{_num(row.get('f1_ci_high'), 3)}</td>"
            f"<td style=\"width:130px\">{f1_bar(row.get('f1', 0), row.get('f1_ci_low', 0), row.get('f1_ci_high', 0))}</td>"
            "</tr>"
        )
    return f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"


def latency_chart(rows: list[dict[str, Any]], *, width: int = 520, height: int = 180) -> str:
    """Bar chart of median page-load latency per configuration."""
    if not rows:
        return ""
    peak = max((float(row.get("p50_ms", 0) or 0) for row in rows), default=0.0) or 1.0
    slot = width / max(1, len(rows))
    bars = []
    for index, row in enumerate(rows):
        value = float(row.get("p50_ms", 0) or 0)
        bar_height = (value / peak) * (height - 40)
        x = index * slot + slot * 0.22
        y = height - 24 - bar_height
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{slot * 0.56:.1f}" '
            f'height="{bar_height:.1f}" rx="4" fill="#1f6feb"></rect>'
            f'<text x="{x + slot * 0.28:.1f}" y="{height - 8}" fill="#7d8794" '
            f'font-size="12" text-anchor="middle">'
            f'{escape(str(row.get("configuration", "?")))}</text>'
            f'<text x="{x + slot * 0.28:.1f}" y="{y - 6:.1f}" fill="#e6edf3" '
            f'font-size="11" text-anchor="middle">{value:.0f} ms</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Median page-load latency per configuration">'
        f"{''.join(bars)}</svg>"
    )


def f1_chart(rows: list[dict[str, Any]], *, width: int = 520, height: int = 200) -> str:
    """Bar chart of observer F1 with the confidence interval as an error bar."""
    if not rows:
        return ""
    slot = width / max(1, len(rows))
    bars = []
    for index, row in enumerate(rows):
        value = float(row.get("f1", 0) or 0)
        low = float(row.get("f1_ci_low", 0) or 0)
        high = float(row.get("f1_ci_high", 0) or 0)
        usable = height - 40
        bar_height = value * usable
        x = index * slot + slot * 0.22
        y = height - 24 - bar_height
        bar_width = slot * 0.56
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
            f'height="{bar_height:.1f}" rx="4" fill="#58a6ff"></rect>'
            f'<line x1="{x + bar_width / 2:.1f}" y1="{height - 24 - low * usable:.1f}" '
            f'x2="{x + bar_width / 2:.1f}" y2="{height - 24 - high * usable:.1f}" '
            f'stroke="#e3b341" stroke-width="2"></line>'
            f'<text x="{x + bar_width / 2:.1f}" y="{height - 8}" fill="#7d8794" '
            f'font-size="12" text-anchor="middle">'
            f'{escape(str(row.get("configuration", "?")))}</text>'
            f'<text x="{x + bar_width / 2:.1f}" y="{max(12, y - 6):.1f}" fill="#e6edf3" '
            f'font-size="11" text-anchor="middle">{value:.3f}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Active/idle classifier F1 per configuration">'
        f"{''.join(bars)}</svg>"
    )


def status_cards(model: DashboardModel) -> str:
    """Live session state and the sizes of the project artefacts."""
    stats = model.stats
    lab = model.lab
    state = str(stats.get("state", "idle"))
    pill = "ok" if state in {"connected", "shaping"} else ("warn" if state == "connecting" else "bad")
    cards = [
        stat_card("Session", state, "lab mode" if lab.get("ready") else "no lab stack running"),
        stat_card("Persona", str(stats.get("persona") or lab.get("persona") or "-"),
                  f"period {stats.get('period', '-')}"),
        stat_card("Real traffic", f"{int(stats.get('real_down', 0) or 0) // 1024} KiB",
                  f"up {int(stats.get('real_up', 0) or 0) // 1024} KiB"),
        stat_card("Cover traffic", f"{int(stats.get('cover_down', 0) or 0) // 1024} KiB",
                  f"overhead {_num(stats.get('overhead', 0))}x"),
        stat_card("Personas", str(len(model.personas)),
                  f"{sum(1 for p in model.personas if p.get('generated'))} generated"),
        stat_card("Runs recorded", str(len(model.runs)), "in the local database"),
    ]
    status = f'<span class="pill {pill}">{escape(state)}</span>'
    return (
        f'<div class="grid">{"".join(cards)}</div>'
        f'<p class="muted" style="margin-top:12px">Engine: {status}</p>'
    )


def runs_table(model_runs: list[dict[str, Any]]) -> str:
    if not model_runs:
        return '<p class="muted">No runs yet.</p>'
    head = (
        "<tr><th>Run</th><th>Config</th><th>Persona</th><th>Started</th>"
        "<th>Duration</th><th>Overhead</th></tr>"
    )
    rows = []
    for run in model_runs:
        run_id = str(run.get("run_id", ""))
        rows.append(
            "<tr>"
            f'<td><a href="/run?id={escape(run_id)}">{escape(run_id)}</a></td>'
            f"<td>{escape(str(run.get('configuration', '')))}</td>"
            f"<td>{escape(str(run.get('persona_id', '')))}</td>"
            f"<td class=\"muted\">{escape(str(run.get('started_at', '')))}</td>"
            f"<td>{_num(run.get('duration_s'), 1)} s</td>"
            f"<td>{_num(run.get('overhead'))}x</td>"
            "</tr>"
        )
    return f"<table><thead>{head}</thead><tbody>{''.join(rows)}</tbody></table>"


def personas_table(personas: list[dict[str, Any]]) -> str:
    if not personas:
        return '<p class="muted">No personas found.</p>'
    head = "<tr><th>Persona</th><th>Source</th><th>States</th><th>Floor slot</th><th>Description</th></tr>"
    rows = []
    for persona in personas:
        badge = "ok" if persona.get("source") in {"static", "clearnet", "llm"} else "warn"
        rows.append(
            "<tr>"
            f"<td><strong>{escape(str(persona.get('id', '')))}</strong></td>"
            f'<td><span class="pill {badge}">{escape(str(persona.get("source", "")))}</span></td>'
            f"<td>{int(persona.get('states', 0) or 0)}</td>"
            f"<td>{escape(str(persona.get('floor_slot_ms', '-')))} ms</td>"
            f"<td class=\"muted\">{escape(str(persona.get('description', '')))}</td>"
            "</tr>"
        )
    return f"<table><thead>{head}</thead><tbody>{''.join(rows)}</tbody></table>"


def overview(model: DashboardModel, *, refresh_s: int | None = None) -> str:
    """The dashboard home page."""
    note = (
        f'<p class="pill warn">database: {escape(model.db_error)}</p>' if model.db_error else ""
    )
    chart_cards = ""
    if model.comparison:
        chart_cards = f"""
<h2>Observer inference</h2>
<div class="card">{f1_chart(model.comparison)}
<p class="muted">Bars: mean F1 of the ACTIVE/IDLE classifiers; whiskers: bootstrap 95% CI.
Lower is better for privacy.</p></div>
<h2>Cost</h2>
<div class="card">{latency_chart(model.comparison)}
<p class="muted">Median page-load latency per configuration. Config A is the baseline.</p></div>
"""
    body = f"""
{status_cards(model)}
{note}
<h2>Configurations</h2>
{comparison_table(model.comparison)}
{chart_cards}
<h2>Runs</h2>
{runs_table(model.runs)}
<h2>Personas</h2>
{personas_table(model.personas)}
<p class="muted" style="margin-top:16px">{escape(summary_text(model))}</p>
"""
    return page("Dashboard", body, refresh_s=refresh_s)


def run_page(detail: dict[str, Any]) -> str:
    """Detail page for one experiment run."""
    loads = detail.get("page_loads") or []
    ok = [row for row in loads if 200 <= int(row.get("status", 0) or 0) < 400]
    stats = [
        stat_card("Page loads", str(len(loads)), f"{len(ok)} succeeded"),
        stat_card("Median load", f"{_num(sorted([r.get('load_ms', 0) for r in ok])[len(ok) // 2] if ok else 0, 0)} ms"),
        stat_card("Overhead", f"{_num(detail.get('overhead'))}x", "wire bytes per page byte"),
        stat_card("Duration", f"{_num(detail.get('duration_s'), 1)} s"),
    ]
    timing = detail.get("slot_timing")
    timing_html = ""
    if timing:
        timing_html = (
            '<h2>Slot timing</h2><div class="grid">'
            + stat_card("p50 error", f"{_num(timing.get('p50_error_ms'), 2)} ms")
            + stat_card("p95 error", f"{_num(timing.get('p95_error_ms'), 2)} ms")
            + stat_card("max error", f"{_num(timing.get('max_error_ms'), 2)} ms")
            + "</div>"
        )
    score_rows = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('classifier', '')))}</td>"
        f"<td>{int(row.get('cv_folds', 0) or 0)}</td>"
        f"<td>{_num(row.get('accuracy'), 3)}</td>"
        f"<td>{_num(row.get('precision'), 3)}</td>"
        f"<td>{_num(row.get('recall'), 3)}</td>"
        f"<td>{_num(row.get('f1'), 3)}</td>"
        f"<td class=\"muted\">{_num(row.get('f1_ci_low'), 3)} - {_num(row.get('f1_ci_high'), 3)}</td>"
        "</tr>"
        for row in detail.get("scores") or []
    )
    scores_html = (
        "<h2>Classifier scores</h2><table><thead><tr><th>Classifier</th><th>Folds</th>"
        "<th>Accuracy</th><th>Precision</th><th>Recall</th><th>F1</th><th>95% CI</th>"
        f"</tr></thead><tbody>{score_rows}</tbody></table>"
        if score_rows
        else '<h2>Classifier scores</h2><p class="muted">This run has no scores yet.</p>'
    )
    load_rows = "".join(
        "<tr>"
        f"<td>{int(row.get('idx', 0) or 0)}</td>"
        f"<td>{escape(str(row.get('site', '')))}</td>"
        f"<td>{int(row.get('status', 0) or 0)}</td>"
        f"<td>{int(row.get('bytes', 0) or 0)}</td>"
        f"<td>{_num(row.get('load_ms'), 0)} ms</td>"
        f"<td class=\"muted\">{escape(str(row.get('error') or ''))}</td>"
        "</tr>"
        for row in loads
    )
    body = f"""
<p class="muted"><a href="/">&larr; back to the dashboard</a></p>
<h2 style="margin-top:8px">Run {escape(str(detail.get('run_id', '')))}</h2>
<p class="muted">configuration <strong>{escape(str(detail.get('configuration', '')))}</strong>,
persona <strong>{escape(str(detail.get('persona_id', '')))}</strong>,
started {escape(str(detail.get('started_at', '')))}
{("&middot; note: " + escape(str(detail.get("notes")))) if detail.get("notes") else ""}</p>
<div class="grid">{''.join(stats)}</div>
{timing_html}
{scores_html}
<h2>Page loads</h2>
<table><thead><tr><th>#</th><th>Site</th><th>Status</th><th>Bytes</th><th>Load</th>
<th>Error</th></tr></thead><tbody>{load_rows}</tbody></table>
"""
    return page(f"Run {detail.get('run_id', '')}", body)


def error_page(message: str, *, status: int = 404) -> str:
    return page(
        f"Error {status}",
        f'<h2>Not found</h2><p>{escape(message)}</p><p><a href="/">&larr; dashboard</a></p>',
    )
