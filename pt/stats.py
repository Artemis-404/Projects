"""One-way stats file (D10, SDD 5.8).

The PT client rewrites ``run/stats.json`` atomically every second.  The control
API and the dashboard only ever *read* it: nothing written by the UI can
influence the scheduler, which is what keeps FR-15 and FR-29 intact.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATS_FILENAME = "stats.json"

CounterSnapshot = Callable[[], dict[str, Any]]


@dataclass(slots=True)
class StatsSnapshot:
    """Shape of ``stats.json`` (SDD 5.8)."""

    t: float
    state: str
    persona: str
    period: str
    real_up: int = 0
    real_down: int = 0
    cover_up: int = 0
    cover_down: int = 0
    overhead: float = 0.0
    bootstrap: int = 0
    session_started: float | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": round(self.t, 3),
            "state": self.state,
            "persona": self.persona,
            "period": self.period,
            "real_up": self.real_up,
            "real_down": self.real_down,
            "cover_up": self.cover_up,
            "cover_down": self.cover_down,
            "overhead": self.overhead,
            "bootstrap": self.bootstrap,
            "session_started": self.session_started,
            "reason": self.reason,
        }


def write_stats_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_stats(path: Path | str) -> dict[str, Any] | None:
    """Read the stats file, returning ``None`` when it is absent or unreadable."""
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def stats_age_s(data: dict[str, Any] | None, now: float | None = None) -> float:
    """Seconds since the stats file was last written (stale detection)."""
    if not data:
        return float("inf")
    stamp = data.get("t")
    if not isinstance(stamp, (int, float)):
        return float("inf")
    return max(0.0, (now if now is not None else time.time()) - float(stamp))


class StatsWriter:
    """Background task that refreshes the stats file every ``interval`` seconds."""

    def __init__(self, path: Path, snapshot: CounterSnapshot, interval: float = 1.0) -> None:
        self.path = path
        self.snapshot = snapshot
        self.interval = max(0.05, float(interval))
        self._task: asyncio.Task[None] | None = None

    def write_once(self) -> dict[str, Any]:
        payload = self.snapshot()
        write_stats_atomic(self.path, payload)
        return payload

    async def run(self) -> None:
        while True:
            # A disk problem must never take the tunnel down.
            with contextlib.suppress(Exception):
                self.write_once()
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="uqab-stats")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
