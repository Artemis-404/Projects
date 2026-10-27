"""One-second window feature extraction (FR-22).

The observer in UQAB sees a single link: packet sizes, timing, direction and IP
addresses.  This module turns a capture into the feature table the classifier
uses: per ``window_s`` window, packet count, byte count, mean and variance of
packet size, mean and variance of inter-packet gap, the up/down direction ratio,
and a burst count.

Windows are labelled from the ground-truth CSV that the experiment runner writes
(FR-20), so ACTIVE and IDLE windows are never guessed from the traffic itself.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from pathlib import Path

from analysis.pcap import Packet

FEATURE_COLUMNS = (
    "packets",
    "bytes",
    "mean_size",
    "var_size",
    "mean_gap_ms",
    "var_gap_ms",
    "up_ratio",
    "bursts",
)

LABELS = ("active", "idle")


@dataclass(frozen=True, slots=True)
class GroundTruthWindow:
    """One ACTIVE/IDLE interval recorded by the experiment script (FR-20)."""

    start: float
    end: float
    label: str
    site: str = ""
    load_ms: float = 0.0

    def to_row(self) -> dict[str, object]:
        return {
            "t_start": round(self.start, 6),
            "t_end": round(self.end, 6),
            "label": self.label,
            "site": self.site,
            "load_ms": round(self.load_ms, 3),
        }


@dataclass(frozen=True, slots=True)
class FeatureWindow:
    start: float
    end: float
    packets: int
    bytes: int
    mean_size: float
    var_size: float
    mean_gap_ms: float
    var_gap_ms: float
    up_ratio: float
    bursts: int
    label: str = ""

    def to_row(self) -> dict[str, object]:
        row: dict[str, object] = {"window_start": round(self.start, 6)}
        for name in FEATURE_COLUMNS:
            value = getattr(self, name)
            row[name] = round(value, 6) if isinstance(value, float) else value
        row["label"] = self.label
        return row

    def vector(self) -> list[float]:
        return [float(getattr(self, name)) for name in FEATURE_COLUMNS]


def _variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


def _bursts(offsets: Sequence[float], sizes: Sequence[int], window_s: float) -> int:
    """Count 1/10th sub-windows whose byte rate is far above the window mean."""
    if not offsets:
        return 0
    bins = max(1, int(round(window_s / 0.1)))
    totals = [0] * bins
    for offset, size in zip(offsets, sizes, strict=False):
        index = min(bins - 1, max(0, int(offset / window_s * bins)))
        totals[index] += size
    mean = sum(totals) / bins
    if mean <= 0:
        return 0
    threshold = 2.0 * mean
    return sum(1 for value in totals if value > threshold)


def extract_windows(
    packets: Iterable[Packet],
    *,
    window_s: float = 1.0,
    bridge_port: int | None = None,
    start: float | None = None,
    end: float | None = None,
    ground_truth: Sequence[GroundTruthWindow] = (),
) -> list[FeatureWindow]:
    """Split the capture into fixed windows and label each one."""
    ordered = sorted(packets, key=lambda p: p.ts)
    if not ordered:
        return []
    if window_s <= 0:
        raise ValueError("window_s must be positive")
    t0 = start if start is not None else ordered[0].ts
    t1 = end if end is not None else ordered[-1].ts
    if t1 <= t0:
        return []

    client = ordered[0].src
    count = int(math.ceil((t1 - t0) / window_s))
    buckets: list[list[Packet]] = [[] for _ in range(max(1, count))]
    for packet in ordered:
        if not t0 <= packet.ts <= t1:
            continue
        index = min(len(buckets) - 1, int((packet.ts - t0) / window_s))
        buckets[index].append(packet)

    windows: list[FeatureWindow] = []
    for index, bucket in enumerate(buckets):
        window_start = t0 + index * window_s
        window_end = window_start + window_s
        sizes = [p.length for p in bucket]
        gaps: list[float] = []
        previous: float | None = None
        for packet in bucket:
            if previous is not None:
                gaps.append((packet.ts - previous) * 1000.0)
            previous = packet.ts
        if bridge_port is not None:
            up = sum(1 for p in bucket if p.dport == bridge_port)
        else:
            up = sum(1 for p in bucket if p.src == client)
        offsets = [p.ts - window_start for p in bucket]
        total = len(bucket)
        windows.append(
            FeatureWindow(
                start=window_start,
                end=window_end,
                packets=total,
                bytes=sum(sizes),
                mean_size=(sum(sizes) / total) if total else 0.0,
                var_size=_variance([float(s) for s in sizes]),
                mean_gap_ms=(sum(gaps) / len(gaps)) if gaps else 0.0,
                var_gap_ms=_variance(gaps),
                up_ratio=(up / total) if total else 0.0,
                bursts=_bursts(offsets, sizes, window_s),
                label=label_for(window_start, window_end, ground_truth),
            )
        )
    return windows


def label_for(
    start: float, end: float, ground_truth: Sequence[GroundTruthWindow]
) -> str:
    """Label a window by the ground-truth interval with the largest overlap."""
    best = ""
    best_overlap = 0.0
    for interval in ground_truth:
        overlap = min(end, interval.end) - max(start, interval.start)
        if overlap > best_overlap:
            best_overlap = overlap
            best = interval.label
    if best_overlap <= 0:
        return ""
    return best


def write_ground_truth(path: Path | str, rows: Iterable[GroundTruthWindow]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["t_start", "t_end", "label", "site", "load_ms"]
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_row())
    return len(rows)


def read_ground_truth(path: Path | str) -> list[GroundTruthWindow]:
    target = Path(path)
    if not target.is_file():
        return []
    out: list[GroundTruthWindow] = []
    with target.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                out.append(
                    GroundTruthWindow(
                        start=float(row["t_start"]),
                        end=float(row["t_end"]),
                        label=str(row["label"]).strip().lower(),
                        site=str(row.get("site", "")),
                        load_ms=float(row.get("load_ms", 0.0) or 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    return out


def write_features(path: Path | str, windows: Iterable[FeatureWindow]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = list(windows)
    columns = ["window_start", *FEATURE_COLUMNS, "label"]
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_row())
    return len(rows)


def read_features(path: Path | str) -> list[FeatureWindow]:
    target = Path(path)
    windows: list[FeatureWindow] = []
    with target.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                windows.append(
                    FeatureWindow(
                        start=float(row["window_start"]),
                        end=float(row["window_start"]) + 1.0,
                        packets=int(float(row["packets"])),
                        bytes=int(float(row["bytes"])),
                        mean_size=float(row["mean_size"]),
                        var_size=float(row["var_size"]),
                        mean_gap_ms=float(row["mean_gap_ms"]),
                        var_gap_ms=float(row["var_gap_ms"]),
                        up_ratio=float(row["up_ratio"]),
                        bursts=int(float(row["bursts"])),
                        label=str(row.get("label", "")).strip().lower(),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    return windows


def summarise(windows: Sequence[FeatureWindow]) -> dict[str, float]:
    """Small summary used by the dashboard and by run logs."""
    if not windows:
        return {"windows": 0, "active": 0, "idle": 0, "bytes": 0}
    return {
        "windows": len(windows),
        "active": sum(1 for w in windows if w.label == "active"),
        "idle": sum(1 for w in windows if w.label == "idle"),
        "bytes": sum(w.bytes for w in windows),
        "mean_bytes_per_window": sum(w.bytes for w in windows) / len(windows),
    }


def feature_names() -> list[str]:
    return [f.name for f in fields(FeatureWindow) if f.name in FEATURE_COLUMNS]
