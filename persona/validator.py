"""Persona limits, validation and static fallback (FR-11, SDD 5.5).

Limits come from ``config/config.yaml``:

* ``record_size`` between ``cover.min_record_size`` and 16,384 B;
* every ``slot_ms`` (including burst slots) at least ``cover.min_slot_ms``;
* every state's ``slot_ms`` at most ``cover.floor_slot_ms`` (FR-08 floor rate);
* ``record_size * 1000 / slot_ms`` at most ``cover.max_bytes_per_s``;
* positive durations and transition rows summing to 1 (structural, FR-10).

Any failure falls back to the built-in Balanced persona rather than raising on
the live path.  ``persona/`` stays free of traffic imports (FR-15).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Config
from persona.model import Persona, PersonaSchemaError, builtin_balanced

MAX_RECORD_SIZE = 16_384


@dataclass(frozen=True, slots=True)
class Limits:
    """Validator thresholds (SDD 5.5)."""

    min_record_size: int = 64
    max_record_size: int = MAX_RECORD_SIZE
    min_slot_ms: int = 10
    floor_slot_ms: int = 500
    max_bytes_per_s: int = 262_144

    @classmethod
    def from_config(cls, cfg: Config) -> Limits:
        return cls(
            min_record_size=cfg.get_int("cover.min_record_size", 64),
            min_slot_ms=cfg.get_int("cover.min_slot_ms", 10),
            floor_slot_ms=cfg.get_int("cover.floor_slot_ms", 500),
            max_bytes_per_s=cfg.get_int("cover.max_bytes_per_s", 262_144),
        )


@dataclass(slots=True)
class ValidationOutcome:
    """Result of validating one persona file."""

    persona: Persona
    errors: list[str] = field(default_factory=list)
    fell_back: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


class PersonaValidationError(ValueError):
    """Raised by strict loaders when a persona breaks the limits."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def validate_limits(persona: Persona, limits: Limits) -> list[str]:
    """Return every limit violation for ``persona`` (empty list means valid)."""
    errors: list[str] = []
    if "import_error" in persona.id:
        errors.append("persona.id must not contain 'import_error'")

    if persona.floor_slot_ms < limits.min_slot_ms:
        errors.append(
            f"floor_slot_ms {persona.floor_slot_ms} is below min_slot_ms {limits.min_slot_ms}"
        )
    if persona.floor_slot_ms > limits.floor_slot_ms:
        errors.append(
            f"floor_slot_ms {persona.floor_slot_ms} exceeds the configured floor "
            f"{limits.floor_slot_ms}"
        )

    if not persona.states:
        errors.append("persona has no states")

    for state in persona.states:
        where = f"state {state.name!r}"
        if state.record_size < limits.min_record_size:
            errors.append(
                f"{where}: record_size {state.record_size} below min_record_size "
                f"{limits.min_record_size}"
            )
        if state.record_size > min(limits.max_record_size, MAX_RECORD_SIZE):
            errors.append(
                f"{where}: record_size {state.record_size} above maximum "
                f"{min(limits.max_record_size, MAX_RECORD_SIZE)}"
            )
        if state.slot_ms < limits.min_slot_ms:
            errors.append(f"{where}: slot_ms {state.slot_ms} below min_slot_ms {limits.min_slot_ms}")
        if state.slot_ms > persona.floor_slot_ms:
            errors.append(
                f"{where}: slot_ms {state.slot_ms} exceeds floor_slot_ms {persona.floor_slot_ms} "
                "(floor rate, FR-08)"
            )
        rate = state.record_size * 1000.0 / state.slot_ms
        if rate > limits.max_bytes_per_s:
            errors.append(
                f"{where}: {rate:.0f} B/s over max_bytes_per_s {limits.max_bytes_per_s}"
            )
        if state.burst is not None:
            burst = state.burst
            if burst.slot_ms < limits.min_slot_ms:
                errors.append(
                    f"{where} burst: slot_ms {burst.slot_ms} below min_slot_ms {limits.min_slot_ms}"
                )
            if burst.slot_ms > persona.floor_slot_ms:
                errors.append(
                    f"{where} burst: slot_ms {burst.slot_ms} exceeds floor_slot_ms "
                    f"{persona.floor_slot_ms}"
                )
            burst_rate = state.record_size * 1000.0 / burst.slot_ms
            if burst_rate > limits.max_bytes_per_s:
                errors.append(
                    f"{where} burst: {burst_rate:.0f} B/s over max_bytes_per_s "
                    f"{limits.max_bytes_per_s}"
                )
            if burst.every_s[1] < 1.0:
                errors.append(f"{where} burst: every_s upper bound {burst.every_s[1]} is too small")
        if state.duration_s[0] <= 0 or state.duration_s[1] <= 0:
            errors.append(f"{where}: duration_s must be positive")

    for name, row in persona.transitions.items():
        total = sum(row.values())
        if abs(total - 1.0) > 1e-6:
            errors.append(f"transitions[{name!r}] sums to {total:.6f}, must sum to 1")
        # Absent targets simply have weight 0, as in the SRS 5.5 example where
        # ``idle`` only lists a transition back to ``reading``.
    missing_rows = set(persona.state_names) - set(persona.transitions)
    if missing_rows:
        errors.append(f"transitions missing row(s) {sorted(missing_rows)}")

    return errors


def validate_dict(data: Any, limits: Limits) -> ValidationOutcome:
    """Validate a parsed persona document, falling back to Balanced on failure."""
    try:
        persona = Persona.from_dict(data)
    except PersonaSchemaError as exc:
        fallback = builtin_balanced()
        return ValidationOutcome(fallback, [f"schema: {exc}"], fell_back=True)

    errors = validate_limits(persona, limits)
    if errors:
        return ValidationOutcome(builtin_balanced(), errors, fell_back=True)
    return ValidationOutcome(persona, [], fell_back=False)


def validate_file(path: str | Path, limits: Limits) -> ValidationOutcome:
    """Validate a persona file, falling back to Balanced on any failure."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return ValidationOutcome(builtin_balanced(), [f"cannot read {p}: {exc}"], fell_back=True)
    try:
        data = json.loads(text)
    except ValueError as exc:
        return ValidationOutcome(
            builtin_balanced(), [f"{p.name}: invalid JSON: {exc}"], fell_back=True
        )
    outcome = validate_dict(data, limits)
    if outcome.ok and outcome.persona.id != p.stem:
        outcome.errors = [f"persona id {outcome.persona.id!r} does not match file name {p.stem!r}"]
        outcome.persona = builtin_balanced()
        outcome.fell_back = True
    return outcome


def load_persona(path: str | Path, limits: Limits) -> Persona:
    """Strict load used where an invalid persona must be reported, not replaced."""
    outcome = validate_file(path, limits)
    if not outcome.ok:
        raise PersonaValidationError(outcome.errors)
    return outcome.persona


def load_by_id(persona_id: str, profiles_dir: str | Path, limits: Limits) -> Persona:
    """Load a persona by id (searching ``profiles/`` then ``profiles/generated/``)."""
    root = Path(profiles_dir)
    for candidate in (root / f"{persona_id}.json", root / "generated" / f"{persona_id}.json"):
        if candidate.is_file():
            return load_persona(candidate, limits)
    raise PersonaValidationError([f"unknown persona {persona_id!r}"])


@dataclass(frozen=True, slots=True)
class PersonaSummary:
    """One row of ``GET /personas`` (SDD 5.7)."""

    id: str
    source: str
    description: str
    path: str
    valid: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "description": self.description,
            "path": self.path,
            "valid": self.valid,
            "errors": list(self.errors),
        }


def discover_personas(profiles_dir: str | Path, limits: Limits) -> list[PersonaSummary]:
    """List every persona file with its validation status (validated ones first)."""
    root = Path(profiles_dir)
    found: list[PersonaSummary] = []
    seen: set[str] = set()
    candidates = sorted(root.glob("*.json")) + sorted((root / "generated").glob("*.json"))
    for path in candidates:
        if not path.is_file():
            continue
        outcome = validate_file(path, limits)
        persona = outcome.persona
        # Only report the persona if it is the one stored in the file; a fallback
        # result means the file itself is invalid.
        valid = outcome.ok
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            stored_id = str(raw.get("id", "")) if isinstance(raw, dict) else ""
            source = str(raw.get("source", "static")) if isinstance(raw, dict) else "static"
            description = str(raw.get("description", "")) if isinstance(raw, dict) else ""
        except (OSError, ValueError):
            stored_id, source, description = path.stem, "static", ""
        if valid and persona.id in seen:
            continue
        if valid:
            seen.add(persona.id)
        found.append(
            PersonaSummary(
                id=persona.id if valid else stored_id or path.stem,
                source=source if valid else "static",
                description=description,
                path=str(path),
                valid=valid,
                errors=tuple(outcome.errors),
            )
        )
    found.sort(key=lambda p: (not p.valid, p.id))
    return found


def validated_personas(profiles_dir: str | Path, limits: Limits) -> list[PersonaSummary]:
    """Only the personas the app may offer in the picker (FR-28)."""
    return [p for p in discover_personas(profiles_dir, limits) if p.valid]


def fallback_persona(limits: Limits) -> Persona:
    """The built-in Balanced persona, guaranteed valid by construction."""
    persona = builtin_balanced()
    errors = validate_limits(persona, limits)
    if errors:  # pragma: no cover - only if config limits are set absurdly low
        raise PersonaValidationError(errors)
    return persona
