"""Deterministic persona timetable (FR-07, FR-13, D6).

Both PT sides build the same slot sequence from the persona and a shared 64-bit
seed, so no timetable is ever sent on the wire.  The timetable is a pure
function of ``(persona, seed)``: it never looks at real traffic, wall-clock time
or user activity, which is what keeps persona changes on a fixed schedule
(FR-13) and persona code unable to read traffic state (FR-15).
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass

from persona.model import Persona

MAX_SEED = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class Slot:
    """One scheduled send opportunity."""

    index: int
    due_s: float
    record_size: int
    state: str
    burst: bool = False
    floor: bool = False


class Timetable:
    """Iterable, deterministic slot schedule for one session and direction."""

    def __init__(self, persona: Persona, seed: int, floor_slot_ms: int | None = None) -> None:
        if not 0 <= seed <= MAX_SEED:
            raise ValueError("seed must be an unsigned 64-bit integer")
        self.persona = persona
        self.seed = seed
        self.floor_slot_ms = int(floor_slot_ms if floor_slot_ms is not None else persona.floor_slot_ms)
        if self.floor_slot_ms <= 0:
            raise ValueError("floor_slot_ms must be positive")

    # ------------------------------------------------------------------ core
    def slots(self, limit: int | None = None) -> Iterator[Slot]:
        """Yield slots in order. ``due_s`` is seconds after the session start."""
        persona = self.persona
        rng = random.Random(self.seed)
        state = persona.state(persona.start_state)
        t = 0.0
        index = 0
        emitted = 0

        while limit is None or emitted < limit:
            duration = rng.uniform(*state.duration_s)
            state_end = t + duration
            next_burst: float | None = None
            if state.burst is not None:
                next_burst = t + rng.uniform(*state.burst.every_s)

            while t < state_end:
                if (
                    state.burst is not None
                    and next_burst is not None
                    and t >= next_burst
                ):
                    count = rng.randint(*state.burst.slots)
                    for _ in range(count):
                        if t >= state_end:
                            break
                        yield Slot(index, round(t, 6), state.record_size, state.name, burst=True)
                        index += 1
                        emitted += 1
                        if limit is not None and emitted >= limit:
                            return
                        t += state.burst.slot_ms / 1000.0
                    next_burst = t + rng.uniform(*state.burst.every_s)
                    continue

                interval_ms = min(state.slot_ms, self.floor_slot_ms)
                yield Slot(index, round(t, 6), state.record_size, state.name)
                index += 1
                emitted += 1
                if limit is not None and emitted >= limit:
                    return
                t += interval_ms / 1000.0

            state = persona.state(persona.sample_next(state.name, rng))

        return

    def take(self, count: int) -> list[Slot]:
        return list(self.slots(limit=count))

    def first(self) -> Slot:
        for slot in self.slots(limit=1):
            return slot
        raise RuntimeError("persona produced no slots")  # pragma: no cover

    def slots_until(self, horizon_s: float) -> list[Slot]:
        """Every slot due within ``horizon_s`` seconds."""
        out: list[Slot] = []
        for slot in self.slots():
            if slot.due_s > horizon_s:
                break
            out.append(slot)
        return out


def session_seed() -> int:
    """Fresh 64-bit session seed (FR-07)."""
    import secrets

    return secrets.randbits(64)


def seeded_rng(seed: int) -> random.Random:
    """Explicit helper so tests can reproduce persona and timetable draws."""
    return random.Random(seed)
