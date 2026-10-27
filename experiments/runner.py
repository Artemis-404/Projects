"""Experiment runner: scripted browsing timeline, captures, labels (FR-20, FR-21).

One run = one configuration (A plain Tor / B constant rate / C persona) on one
persona, replaying a fixed script of page loads with ACTIVE/IDLE phases.  The
runner drives the real PT client through SOCKS5 and the lab mock Tor, records
per-load page metrics, writes the ground-truth CSV used by the classifiers, and
stores the run in SQLite (SDD 6.3).
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from experiments.socks_http import HttpError, PageLoad, fetch_sync

CONFIGURATIONS = ("A", "B", "C")

CONFIGURATION_NOTES = {
    "A": "plain Tor (no cover traffic)",
    "B": "constant-rate cover (static Constant persona)",
    "C": "persona cover (timetable driven)",
}


class ExperimentError(RuntimeError):
    """The run could not be carried out (stack down, bad script, ...)."""


@dataclass(slots=True)
class Phase:
    """One scripted step: LOAD fetches a URL, IDLE just waits."""

    kind: str
    duration_s: float
    url: str = ""
    site: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"load", "idle"}:
            raise ExperimentError(f"unknown phase kind {self.kind!r}")
        if self.duration_s < 0:
            raise ExperimentError("phase duration must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "duration_s": self.duration_s,
            "url": self.url,
            "site": self.site,
            "label": self.label,
        }


@dataclass(slots=True)
class LoadResult:
    index: int
    site: str
    url: str
    status: int
    bytes: int
    ms: float
    started: float
    finished: float
    error: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "site": self.site,
            "url": self.url,
            "status": self.status,
            "bytes": self.bytes,
            "load_ms": round(self.ms, 3),
            "error": self.error,
        }

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status < 400


@dataclass(slots=True)
class Timeline:
    """The scripted browsing session replayed by every configuration."""

    phases: list[Phase] = field(default_factory=list)

    @property
    def total_s(self) -> float:
        return sum(phase.duration_s for phase in self.phases)

    def labels(self) -> list[tuple[float, float, str]]:
        out: list[tuple[float, float, str]] = []
        cursor = 0.0
        for phase in self.phases:
            out.append((cursor, cursor + phase.duration_s, phase.label or phase.kind))
            cursor += phase.duration_s
        return out


def standard_timeline(
    *,
    sites: Sequence[str] = ("demo.uqab",),
    loads: int = 30,
    load_s: float = 1.0,
    idle_s: float = 3.0,
    paths: Sequence[str] = ("/", "/docs"),
) -> Timeline:
    """Alternate ACTIVE page loads with IDLE pauses, matching NFR-01's 30 loads."""
    phases: list[Phase] = []
    for index in range(loads):
        site = sites[index % len(sites)]
        path = paths[index % len(paths)]
        phases.append(
            Phase(
                kind="load",
                duration_s=load_s,
                url=f"http://{site}{path}",
                site=site,
                label="active",
            )
        )
        phases.append(Phase(kind="idle", duration_s=idle_s, site=site, label="idle"))
    return Timeline(phases=phases)


def load_sites(path: Path | str) -> list[str]:
    """Read a site list (one host per line, ``#`` comments) from a file."""
    source = Path(path)
    if not source.is_file():
        raise ExperimentError(f"site list not found: {source}")
    sites: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            sites.append(entry)
    if not sites:
        raise ExperimentError(f"site list is empty: {source}")
    return sites


def port_open(host: str, port: int, *, wait_s: float = 0.0, step_s: float = 0.05) -> bool:
    """Wait until a TCP port accepts connections (bounded by ``wait_s``)."""
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(step_s)


@dataclass(slots=True)
class ReplayOutcome:
    """Loads plus the ground-truth intervals and the replay's time origin.

    ``phases`` intervals are relative to the start of the replay; the capture
    timestamps are ``started_wall + offset`` so both share one timeline.
    """

    loads: list[LoadResult]
    phases: list[dict[str, Any]]
    started_mono: float
    started_wall: float

    def total_s(self) -> float:
        last = self.phases[-1]["t_end"] if self.phases else 0.0
        return float(last)


def replay(
    timeline: Timeline,
    *,
    proxy_host: str,
    proxy_port: int,
    fetch_fn: Any = None,
    should_stop: Any = None,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
) -> ReplayOutcome:
    """Replay the timeline through the proxy, timing every load.

    The returned ``phases`` carry intervals relative to the start of the replay,
    ready to be written as ground truth for the capture.
    """
    fetcher = fetch_fn or fetch_sync
    loads: list[LoadResult] = []
    phase_rows: list[dict[str, Any]] = []
    start = clock()
    cursor = start
    load_index = 0
    for phase in timeline.phases:
        if should_stop is not None and should_stop():
            break
        phase_start = cursor
        if phase.kind == "load":
            load_index += 1
            began = clock()
            error = ""
            status = 0
            size = 0
            try:
                loaded: PageLoad = fetcher(
                    phase.url,
                    proxy_host,
                    proxy_port,
                    timeout=max(5.0, phase.duration_s * 5),
                )
                status = int(loaded.status)
                size = int(loaded.bytes_received)
            except HttpError as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - the run records any failure
                error = f"{type(exc).__name__}: {exc}"
            finished = clock()
            loads.append(
                LoadResult(
                    index=load_index,
                    site=phase.site,
                    url=phase.url,
                    status=status,
                    bytes=size,
                    ms=(finished - began) * 1000.0,
                    started=began,
                    finished=finished,
                    error=error,
                )
            )
        # ``duration_s`` is dwell time: the script holds each phase for its full
        # length so ACTIVE and IDLE intervals have the length the labels claim.
        remaining = phase.duration_s - (clock() - phase_start)
        if remaining > 0:
            sleep(remaining)
        cursor = phase_start + phase.duration_s
        phase_rows.append(
            {
                "t_start": round(phase_start - start, 6),
                "t_end": round(cursor - start, 6),
                "label": phase.label or phase.kind,
                "site": phase.site,
                "load_ms": round(loads[-1].ms, 3) if phase.kind == "load" and loads else 0.0,
            }
        )
    return ReplayOutcome(
        loads=loads, phases=phase_rows, started_mono=start, started_wall=time.time()
    )


def summarise(loads: Iterable[LoadResult]) -> dict[str, Any]:
    """Page-load statistics used by the dashboard and the SQLite rows."""
    rows = list(loads)
    ok = [row for row in rows if row.ok]
    times = sorted(row.ms for row in ok)
    total_bytes = sum(row.bytes for row in rows)
    return {
        "loads": len(rows),
        "ok": len(ok),
        "failed": len(rows) - len(ok),
        "bytes": total_bytes,
        "p50_ms": times[len(times) // 2] if times else 0.0,
        "p95_ms": times[min(len(times) - 1, int(len(times) * 0.95))] if times else 0.0,
        "max_ms": times[-1] if times else 0.0,
        "mean_ms": (sum(times) / len(times)) if times else 0.0,
    }


def wait_and_replay(
    timeline: Timeline,
    *,
    proxy_host: str,
    proxy_port: int,
    wait_s: float = 20.0,
) -> ReplayOutcome:
    """Wait for the SOCKS port to open, then replay the timeline."""
    if not port_open(proxy_host, proxy_port, wait_s=wait_s):
        raise ExperimentError(f"SOCKS proxy {proxy_host}:{proxy_port} never opened")
    return replay(timeline, proxy_host=proxy_host, proxy_port=proxy_port)
