"""Persona data model (FR-10, SDD 5.5).

``persona/`` is data plus validation only: it must never import ``pt/``,
``tunnel/``, ``cover/`` or any scheduler queue (FR-15, D7).  The model here is a
plain value object so both the offline generators and the live timetable can
share it without a dependency on traffic code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


class PersonaSchemaError(ValueError):
    """Structural problem in a persona document (FR-10)."""


def _require(obj: dict[str, Any], key: str, kind: type | tuple[type, ...], where: str) -> Any:
    if key not in obj:
        raise PersonaSchemaError(f"{where}: missing required field {key!r}")
    value = obj[key]
    if kind is int and isinstance(value, bool):
        raise PersonaSchemaError(f"{where}.{key}: expected integer, got boolean")
    if not isinstance(value, kind):
        raise PersonaSchemaError(f"{where}.{key}: expected {kind}, got {type(value).__name__}")
    return value


def _number_pair(value: Any, where: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PersonaSchemaError(f"{where}: expected a two-element range")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise PersonaSchemaError(f"{where}: range values must be numbers")
    low, high = float(value[0]), float(value[1])
    if low <= 0 or high <= 0:
        raise PersonaSchemaError(f"{where}: range values must be positive")
    if high < low:
        raise PersonaSchemaError(f"{where}: range end {high} is below start {low}")
    return low, high


def _int_pair(value: Any, where: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PersonaSchemaError(f"{where}: expected a two-element range")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise PersonaSchemaError(f"{where}: range values must be integers")
    low, high = int(value[0]), int(value[1])
    if low <= 0 or high <= 0:
        raise PersonaSchemaError(f"{where}: range values must be positive")
    if high < low:
        raise PersonaSchemaError(f"{where}: range end {high} is below start {low}")
    return low, high


@dataclass(frozen=True, slots=True)
class Burst:
    """A short burst of faster slots inside a state."""

    every_s: tuple[float, float]
    slots: tuple[int, int]
    slot_ms: int

    @classmethod
    def from_dict(cls, data: Any, where: str) -> Burst:
        if not isinstance(data, dict):
            raise PersonaSchemaError(f"{where}: burst must be an object")
        every = _number_pair(data.get("every_s"), f"{where}.every_s")
        slots = _int_pair(data.get("slots"), f"{where}.slots")
        slot_ms = _require(data, "slot_ms", int, where)
        if slot_ms <= 0:
            raise PersonaSchemaError(f"{where}.slot_ms must be positive")
        return cls(every_s=every, slots=slots, slot_ms=slot_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "every_s": [self.every_s[0], self.every_s[1]],
            "slots": [self.slots[0], self.slots[1]],
            "slot_ms": self.slot_ms,
        }


@dataclass(frozen=True, slots=True)
class State:
    """One persona state: a record size, a slot interval and a duration range."""

    name: str
    record_size: int
    slot_ms: int
    duration_s: tuple[float, float]
    burst: Burst | None = None

    @classmethod
    def from_dict(cls, data: Any, where: str) -> State:
        if not isinstance(data, dict):
            raise PersonaSchemaError(f"{where}: state must be an object")
        name = _require(data, "name", str, where)
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            raise PersonaSchemaError(f"{where}.name: use letters, digits, '_' or '-'")
        record_size = _require(data, "record_size", int, where)
        slot_ms = _require(data, "slot_ms", int, where)
        if record_size <= 0:
            raise PersonaSchemaError(f"{where}.record_size must be positive")
        if slot_ms <= 0:
            raise PersonaSchemaError(f"{where}.slot_ms must be positive")
        duration = _number_pair(data.get("duration_s"), f"{where}.duration_s")
        burst = None
        if data.get("burst") is not None:
            burst = Burst.from_dict(data["burst"], f"{where}.burst")
        return cls(name, record_size, slot_ms, duration, burst)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "record_size": self.record_size,
            "slot_ms": self.slot_ms,
            "duration_s": [self.duration_s[0], self.duration_s[1]],
        }
        if self.burst is not None:
            out["burst"] = self.burst.to_dict()
        return out


@dataclass(frozen=True, slots=True)
class Persona:
    """A validated persona policy (SDD 5.5)."""

    id: str
    version: int
    source: str
    description: str
    floor_slot_ms: int
    start_state: str
    states: tuple[State, ...]
    transitions: dict[str, dict[str, float]]

    # ---------------------------------------------------------------- parsing
    @classmethod
    def from_dict(cls, data: Any) -> Persona:
        if not isinstance(data, dict):
            raise PersonaSchemaError("persona must be a JSON object")
        pid = _require(data, "id", str, "persona")
        if not pid or not pid.replace("_", "").replace("-", "").isalnum():
            raise PersonaSchemaError("persona.id: use letters, digits, '_' or '-'")
        version = _require(data, "version", int, "persona")
        if version < 1:
            raise PersonaSchemaError("persona.version must be >= 1")
        source = _require(data, "source", str, "persona")
        if source not in {"static", "llm", "clearnet"}:
            raise PersonaSchemaError(f"persona.source {source!r} must be static, llm or clearnet")
        description = data.get("description", "")
        if not isinstance(description, str):
            raise PersonaSchemaError("persona.description must be a string")
        floor_slot_ms = _require(data, "floor_slot_ms", int, "persona")
        if floor_slot_ms <= 0:
            raise PersonaSchemaError("persona.floor_slot_ms must be positive")
        start_state = _require(data, "start_state", str, "persona")

        raw_states = data.get("states")
        if not isinstance(raw_states, list) or not raw_states:
            raise PersonaSchemaError("persona.states must be a non-empty list")
        states = tuple(
            State.from_dict(item, f"persona.states[{i}]") for i, item in enumerate(raw_states)
        )
        names = [s.name for s in states]
        if len(set(names)) != len(names):
            raise PersonaSchemaError("persona.states: duplicate state name")
        if start_state not in names:
            raise PersonaSchemaError(f"persona.start_state {start_state!r} is not a state")

        raw_transitions = data.get("transitions")
        if not isinstance(raw_transitions, dict):
            raise PersonaSchemaError("persona.transitions must be an object")
        transitions: dict[str, dict[str, float]] = {}
        for name in names:
            row = raw_transitions.get(name)
            if not isinstance(row, dict):
                raise PersonaSchemaError(f"persona.transitions.{name} is missing")
            unknown = set(row) - set(names)
            if unknown:
                raise PersonaSchemaError(
                    f"persona.transitions.{name}: unknown target(s) {sorted(unknown)}"
                )
            parsed: dict[str, float] = {}
            for target, weight in row.items():
                if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                    raise PersonaSchemaError(
                        f"persona.transitions.{name}.{target} must be a number"
                    )
                if weight < 0:
                    raise PersonaSchemaError(
                        f"persona.transitions.{name}.{target} must not be negative"
                    )
                parsed[target] = float(weight)
            total = sum(parsed.values())
            if abs(total - 1.0) > 1e-6:
                raise PersonaSchemaError(
                    f"persona.transitions.{name} sums to {total:.6f}, must sum to 1"
                )
            transitions[name] = parsed

        return cls(
            id=pid,
            version=version,
            source=source,
            description=description,
            floor_slot_ms=floor_slot_ms,
            start_state=start_state,
            states=states,
            transitions=transitions,
        )

    @classmethod
    def from_json(cls, text: str) -> Persona:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise PersonaSchemaError(f"persona is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    # ------------------------------------------------------------- rendering
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "source": self.source,
            "description": self.description,
            "floor_slot_ms": self.floor_slot_ms,
            "start_state": self.start_state,
            "states": [s.to_dict() for s in self.states],
            "transitions": {k: dict(v) for k, v in self.transitions.items()},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n"

    # -------------------------------------------------------------- helpers
    def state(self, name: str) -> State:
        for state in self.states:
            if state.name == name:
                return state
        raise KeyError(name)

    @property
    def state_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.states)

    def content_hash(self) -> str:
        """SHA-256 over the canonical persona content.

        FR-07 needs both ends to agree on the persona.  Canonical JSON means a
        whitespace-only edit on one machine cannot desynchronise a session.
        """
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def sample_next(self, current: str, rng: Any) -> str:
        """Draw the next state from ``transitions[current]`` using ``rng.random()``."""
        row = self.transitions[current]
        draw = rng.random()
        cumulative = 0.0
        last = None
        for target in sorted(row):
            weight = row[target]
            if weight <= 0:
                continue
            cumulative += weight
            last = target
            if draw < cumulative:
                return target
        return last if last is not None else current

    def all_states(self) -> Iterable[State]:
        return self.states


def builtin_balanced() -> Persona:
    """The built-in fallback persona required by FR-11."""
    return Persona.from_dict(
        {
            "id": "balanced",
            "version": 1,
            "source": "static",
            "description": "Built-in fallback: steady reading with occasional bursts",
            "floor_slot_ms": 500,
            "start_state": "reading",
            "states": [
                {
                    "name": "reading",
                    "record_size": 1024,
                    "slot_ms": 100,
                    "duration_s": [30, 120],
                    "burst": {"every_s": [10, 40], "slots": [5, 20], "slot_ms": 40},
                },
                {
                    "name": "idle",
                    "record_size": 1024,
                    "slot_ms": 400,
                    "duration_s": [60, 300],
                },
            ],
            "transitions": {
                "reading": {"reading": 0.3, "idle": 0.7},
                "idle": {"reading": 1.0},
            },
        }
    )
