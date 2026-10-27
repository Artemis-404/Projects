"""Timetable tests (FR-06, FR-07, FR-08, FR-13, D6)."""

from __future__ import annotations

from collections import Counter

import pytest

from config import load_config
from cover.timetable import MAX_SEED, Timetable, session_seed
from persona.model import Persona, builtin_balanced
from persona.validator import Limits, load_persona


@pytest.fixture(scope="module")
def limits() -> Limits:
    return Limits.from_config(load_config())


@pytest.fixture(scope="module")
def balanced(limits: Limits) -> Persona:
    return load_persona("persona/profiles/balanced.json", limits)


def test_same_seed_and_persona_give_identical_slots(balanced: Persona) -> None:
    a = Timetable(balanced, 123456789).take(500)
    b = Timetable(balanced, 123456789).take(500)
    assert a == b


def test_different_seed_changes_the_schedule(balanced: Persona) -> None:
    a = Timetable(balanced, 1).take(2000)
    b = Timetable(balanced, 2).take(2000)
    assert [s.record_size for s in a] != [s.record_size for s in b] or [
        s.due_s for s in a
    ] != [s.due_s for s in b]


def test_slots_are_monotonic_and_indexed(balanced: Persona) -> None:
    slots = Timetable(balanced, 42).take(300)
    assert [s.index for s in slots] == list(range(300))
    times = [s.due_s for s in slots]
    assert times == sorted(times)
    assert times[0] >= 0.0


def test_record_sizes_come_only_from_persona_states(balanced: Persona) -> None:
    allowed = {s.record_size for s in balanced.states}
    for slot in Timetable(balanced, 7).take(400):
        assert slot.record_size in allowed
        assert slot.state in balanced.state_names


def test_floor_rate_is_never_breached(balanced: Persona) -> None:
    slots = Timetable(balanced, 2024).take(600)
    gaps = [b.due_s - a.due_s for a, b in zip(slots, slots[1:], strict=False)]
    assert gaps, "expected multiple slots"
    assert max(gaps) <= balanced.floor_slot_ms / 1000.0 + 1e-9


def test_floor_slot_ms_argument_caps_state_intervals(balanced: Persona) -> None:
    slots = Timetable(balanced, 11, floor_slot_ms=150).take(300)
    gaps = [b.due_s - a.due_s for a, b in zip(slots, slots[1:], strict=False)]
    assert max(gaps) <= 0.15 + 1e-9


def test_state_durations_stay_inside_persona_ranges(balanced: Persona) -> None:
    slots = Timetable(balanced, 3141).take(2000)
    runs: list[tuple[str, float]] = []
    current = slots[0].state
    start = slots[0].due_s
    for slot in slots[1:]:
        if slot.state != current:
            runs.append((current, slot.due_s - start))
            current, start = slot.state, slot.due_s
    runs.append((current, slots[-1].due_s - start))
    for state_name, duration in runs:
        low, high = balanced.state(state_name).duration_s
        # A state can only end after its drawn duration; the sampling step means
        # the measured run is at most one slot interval longer than the draw.
        assert duration >= low - 1e-9
        assert duration <= high + 1.0


def test_persona_succession_follows_the_transition_matrix() -> None:
    persona = Persona.from_dict(
        {
            "id": "deterministic",
            "version": 1,
            "source": "static",
            "floor_slot_ms": 500,
            "start_state": "a",
            "states": [
                {"name": "a", "record_size": 128, "slot_ms": 100, "duration_s": [1, 1]},
                {"name": "b", "record_size": 256, "slot_ms": 100, "duration_s": [1, 1]},
            ],
            "transitions": {"a": {"b": 1.0}, "b": {"a": 1.0}},
        }
    )
    slots = Timetable(persona, 5).take(45)
    order = [s.state for s in slots]
    # 1 s states at a 100 ms slot interval = runs of ten slots, strictly alternating.
    runs: list[str] = []
    for state in order:
        if not runs or runs[-1] != state:
            runs.append(state)
    assert runs == ["a", "b", "a", "b", "a"]
    assert len({sum(1 for s in slots if s.state == name) for name in ("a", "b")}) <= 2


def test_burst_slots_use_the_burst_interval() -> None:
    persona = Persona.from_dict(
        {
            "id": "bursty",
            "version": 1,
            "source": "static",
            "floor_slot_ms": 500,
            "start_state": "chat",
            "states": [
                {
                    "name": "chat",
                    "record_size": 512,
                    "slot_ms": 100,
                    "duration_s": [600, 600],
                    "burst": {"every_s": [5, 5], "slots": [4, 4], "slot_ms": 25},
                }
            ],
            "transitions": {"chat": {"chat": 1.0}},
        }
    )
    slots = Timetable(persona, 8).take(400)
    burst_slots = [s for s in slots if s.burst]
    assert len(burst_slots) > 0
    gaps = [b.due_s - a.due_s for a, b in zip(slots, slots[1:], strict=False)]
    assert min(gaps) == pytest.approx(0.025)


def test_timetable_is_pure_and_repeatable_across_instances(balanced: Persona) -> None:
    first = list(Timetable(balanced, 777).slots(limit=50))
    second = list(Timetable(balanced, 777).slots(limit=50))
    assert first == second


def test_seed_bounds() -> None:
    persona = builtin_balanced()
    with pytest.raises(ValueError):
        Timetable(persona, -1)
    with pytest.raises(ValueError):
        Timetable(persona, MAX_SEED + 1)


def test_session_seed_is_64_bit_random() -> None:
    seeds = {session_seed() for _ in range(50)}
    assert len(seeds) == 50
    assert all(0 <= s <= MAX_SEED for s in seeds)


def test_constant_persona_has_one_state_and_fixed_rate(limits: Limits) -> None:
    persona = load_persona("persona/profiles/constant.json", limits)
    slots = Timetable(persona, 3).take(100)
    assert {s.state for s in slots} == {"constant"}
    assert len({s.record_size for s in slots}) == 1
    assert len(Counter(round(b.due_s - a.due_s, 6) for a, b in zip(slots, slots[1:], strict=False))) == 1
