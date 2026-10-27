"""k-means states and a Markov transition model (FR-18, SDD 5.6).

Input: the aggregate per-window features produced by ``capture_features``.
Output: ``profile.json`` with cluster states, a row-normalised transition matrix
and size/gap histograms -- aggregates only, no URLs, host names or payloads.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Sequence
from pathlib import Path
from profile.privacy import check_profile
from typing import Any

from analysis.features import FeatureWindow

PROFILE_VERSION = 1
SIZE_EDGES = [0, 100, 200, 400, 800, 1200, 1600, 2200, 3000, 5000, 10000, 20000]
GAP_EDGES_MS = [0, 5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000]


class ProfileError(ValueError):
    """The features cannot produce a valid profile."""


def _point(window: FeatureWindow) -> list[float]:
    return [float(window.bytes), float(window.mean_size), float(window.mean_gap_ms)]


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))


def kmeans(
    points: Sequence[Sequence[float]], k: int, *, seed: int = 7, iterations: int = 60
) -> tuple[list[list[float]], list[int]]:
    """Plain Lloyd k-means with deterministic k-means++ seeding."""
    if not points:
        raise ProfileError("no features to cluster")
    k = max(1, min(k, len(points)))
    rng = random.Random(seed)
    centroids: list[list[float]] = [list(points[rng.randrange(len(points))])]
    while len(centroids) < k:
        distances = [min(_distance(p, c) for c in centroids) ** 2 for p in points]
        total = sum(distances)
        if total <= 0:
            centroids.append(list(points[rng.randrange(len(points))]))
            continue
        pick = rng.random() * total
        cumulative = 0.0
        chosen = len(points) - 1
        for index, weight in enumerate(distances):
            cumulative += weight
            if cumulative >= pick:
                chosen = index
                break
        centroids.append(list(points[chosen]))

    labels = [0] * len(points)
    for _ in range(iterations):
        changed = False
        for index, point in enumerate(points):
            best = min(range(len(centroids)), key=lambda c: _distance(point, centroids[c]))
            if labels[index] != best:
                labels[index] = best
                changed = True
        for cluster in range(len(centroids)):
            members = [points[i] for i, label in enumerate(labels) if label == cluster]
            if members:
                centroids[cluster] = [
                    sum(member[axis] for member in members) / len(members)
                    for axis in range(len(members[0]))
                ]
        if not changed:
            break
    return centroids, labels


def _histogram(values: Sequence[float], edges: Sequence[float]) -> dict[str, Any]:
    counts: list[int] = [0] * (len(edges) - 1)
    for value in values:
        for index in range(len(edges) - 1):
            if edges[index] <= value < edges[index + 1]:
                counts[index] += 1
                break
        else:
            if value >= edges[-1]:
                counts[-1] += 1
    return {"edges": [float(e) for e in edges], "counts": counts}


def transition_matrix(labels: Sequence[int], k: int) -> list[list[float]]:
    """Row-normalised Markov matrix over consecutive windows."""
    counts = [[0] * k for _ in range(k)]
    for previous, current in zip(labels, labels[1:], strict=False):
        counts[previous][current] += 1
    matrix: list[list[float]] = []
    for row in counts:
        total = sum(row)
        if total == 0:
            # A state that is never left keeps itself: a self-loop keeps rows valid.
            matrix.append([1.0 if index == row.index(max(row)) else 0.0 for index in range(k)])
            continue
        matrix.append([value / total for value in row])
    return matrix


def learn(
    windows: Sequence[FeatureWindow],
    *,
    k: int = 4,
    seed: int = 7,
) -> dict[str, Any]:
    """Build the profile document described in SDD 5.6."""
    if not windows:
        raise ProfileError("no windows supplied")
    k = max(2, min(k, len(windows)))
    points = [_point(window) for window in windows]
    centroids, labels = kmeans(points, k, seed=seed)
    states = [
        {
            "id": index,
            "bytes_per_s": round(centroid[0], 3),
            "mean_packet_size": round(centroid[1], 3),
            "mean_gap_ms": round(centroid[2], 3),
        }
        for index, centroid in enumerate(centroids)
    ]
    profile = {
        "version": PROFILE_VERSION,
        "k": len(states),
        "states": states,
        "transition_matrix": [
            [round(value, 6) for value in row] for row in transition_matrix(labels, len(states))
        ],
        "size_histogram": _histogram(
            [window.mean_size for window in windows if window.mean_size > 0], SIZE_EDGES
        ),
        "gap_histogram": _histogram(
            [window.mean_gap_ms for window in windows if window.mean_gap_ms > 0], GAP_EDGES_MS
        ),
        "source": "clearnet",
        "windows": len(windows),
    }
    findings = check_profile(profile)
    if findings:
        raise ProfileError("profile violates the privacy rules: " + "; ".join(findings))
    return profile


def write_profile(path: Path | str, profile: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return target


def load_profile(path: Path | str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProfileError("profile root must be an object")
    findings = check_profile(data)
    if findings:
        raise ProfileError("refusing to load an unsafe profile: " + "; ".join(findings))
    return data


def learn_from_capture_features(
    windows: Sequence[FeatureWindow],
    *,
    output: Path | str,
    k: int = 4,
    seed: int = 7,
) -> dict[str, Any]:
    """Convenience: learn a profile and write it to ``output``."""
    profile = learn(windows, k=k, seed=seed)
    write_profile(output, profile)
    return profile
