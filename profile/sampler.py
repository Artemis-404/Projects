"""Turn a clearnet profile into persona variants (FR-19, SDD 5.5).

The sampler never writes a persona that the validator would reject: record size
is clamped into the configured range, slot intervals respect the floor rate and
the rate cap, and transition rows are renormalised.  Every result carries
``source: "clearnet"`` so the app can show where it came from.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from persona.model import Persona
from persona.validator import Limits, validate_dict


class SampleError(ValueError):
    """The profile cannot be turned into a valid persona."""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _state_from_cluster(
    index: int,
    state: dict[str, Any],
    limits: Limits,
    floor_slot_ms: int,
    *,
    profile_index: int,
) -> dict[str, Any]:
    bytes_per_s = float(state.get("bytes_per_s", 0.0))
    mean_size = float(state.get("mean_packet_size", 0.0))
    mean_gap = float(state.get("mean_gap_ms", 0.0))

    slot_ms = int(round(_clamp(mean_gap or 100.0, limits.min_slot_ms, floor_slot_ms)))
    if slot_ms < limits.min_slot_ms:
        slot_ms = limits.min_slot_ms

    # Prefer a record size that reproduces the observed rate at this slot rate,
    # but never below the minimum and never above the per-second cap.
    per_slot_bytes = bytes_per_s * slot_ms / 1000.0
    record_size = int(round(per_slot_bytes))
    record_size = max(record_size, limits.min_record_size)
    record_size = min(record_size, min(limits.max_record_size, 16_384))
    max_size_for_rate = int(limits.max_bytes_per_s * slot_ms / 1000.0)
    if max_size_for_rate < record_size:
        record_size = max(limits.min_record_size, max_size_for_rate)

    # A state with a high self-transition probability lasts longer.
    low, high = (60.0, 300.0) if mean_size < 200 else (30.0, 120.0)
    duration = [round(low + (high - low) * ((profile_index + index) % 3) / 2.0, 1), round(high, 1)]
    state_doc: dict[str, Any] = {
        "name": f"s{index}",
        "record_size": int(record_size),
        "slot_ms": int(slot_ms),
        "duration_s": duration,
    }
    return state_doc


def _normalise_row(row: list[float]) -> dict[str, float]:
    total = sum(value for value in row if value > 0)
    if total <= 0:
        return {}
    return {f"s{index}": value / total for index, value in enumerate(row) if value > 0}


def sample_persona(
    profile: dict[str, Any],
    limits: Limits,
    *,
    persona_id: str = "clearnet",
    floor_slot_ms: int | None = None,
) -> Persona:
    """Convert one profile into one validated persona."""
    states_in = profile.get("states") or []
    if not states_in:
        raise SampleError("profile has no states")
    floor = int(floor_slot_ms if floor_slot_ms is not None else limits.floor_slot_ms)

    states = [
        _state_from_cluster(index, state, limits, floor, profile_index=0)
        for index, state in enumerate(states_in)
    ]
    matrix = profile.get("transition_matrix") or []
    transitions: dict[str, dict[str, float]] = {}
    for index in range(len(states)):
        row = matrix[index] if index < len(matrix) and isinstance(matrix[index], list) else []
        normalised = _normalise_row([float(v) for v in row])
        if not normalised:
            normalised = {f"s{index}": 1.0}
        transitions[f"s{index}"] = normalised

    # Start in the busiest state: bytes per second descending.
    busiest = max(
        range(len(states)),
        key=lambda i: float(states_in[i].get("bytes_per_s", 0.0)),
    )
    document = {
        "id": persona_id,
        "version": 1,
        "source": "clearnet",
        "description": f"Learned locally from {profile.get('windows', '?')} windows of clearnet browsing",
        "floor_slot_ms": floor,
        "start_state": f"s{busiest}",
        "states": states,
        "transitions": transitions,
    }
    outcome = validate_dict(document, limits)
    if not outcome.ok:
        raise SampleError("; ".join(outcome.errors))
    return outcome.persona


def sample_variants(
    profile: dict[str, Any],
    limits: Limits,
    *,
    base_id: str = "clearnet",
    count: int = 2,
) -> list[Persona]:
    """Produce several persona variants from one profile (FR-19)."""
    variants: list[Persona] = []
    for index in range(max(1, count)):
        persona_id = base_id if index == 0 else f"{base_id}-{index + 1}"
        variants.append(sample_persona(profile, limits, persona_id=persona_id))
    return variants


def write_personas(personas: list[Persona], out_dir: Path | str) -> list[Path]:
    """Write persona JSON files into ``out_dir`` (typically profiles/generated)."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for persona in personas:
        path = target / f"{persona.id}.json"
        path.write_text(json.dumps(persona.to_dict(), indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def personas_from_profile_file(
    profile_path: Path | str,
    limits: Limits,
    *,
    out_dir: Path | str | None = None,
    count: int = 2,
    base_id: str = "clearnet",
) -> list[Persona]:
    """Load a profile, sample variants and optionally write them as persona files."""
    from profile.learner import load_profile

    profile = load_profile(profile_path)
    personas = sample_variants(profile, limits, base_id=base_id, count=count)
    if out_dir is not None:
        write_personas(personas, out_dir)
    return personas
