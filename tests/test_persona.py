"""Persona schema, limits and fallback tests (FR-10 to FR-12)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from config import load_config
from persona.model import Persona, PersonaSchemaError, builtin_balanced
from persona.validator import (
    Limits,
    PersonaValidationError,
    discover_personas,
    fallback_persona,
    load_by_id,
    load_persona,
    validate_dict,
    validate_file,
    validated_personas,
)


@pytest.fixture(scope="module")
def limits() -> Limits:
    return Limits.from_config(load_config())


@pytest.fixture(scope="module")
def profiles_dir() -> Path:
    return Path("persona/profiles")


def test_all_shipped_personas_pass_schema_and_limits(limits: Limits, profiles_dir: Path) -> None:
    names = ["low", "balanced", "high", "constant"]
    for name in names:
        outcome = validate_file(profiles_dir / f"{name}.json", limits)
        assert outcome.ok, (name, outcome.errors)
        assert isinstance(outcome.persona, Persona)
        assert outcome.persona.id == name


def test_required_static_personas_exist(limits: Limits, profiles_dir: Path) -> None:
    ids = {p.id for p in validated_personas(profiles_dir, limits)}
    assert {"low", "balanced", "high", "constant"} <= ids


def test_persona_json_roundtrip_is_stable(limits: Limits) -> None:
    persona = load_persona("persona/profiles/high.json", limits)
    again = Persona.from_json(persona.to_json())
    assert again == persona
    assert again.content_hash() == persona.content_hash()


def test_content_hash_changes_with_any_policy_change(limits: Limits) -> None:
    data = json.loads(Path("persona/profiles/balanced.json").read_text())
    original = Persona.from_dict(data)
    data["states"][0]["slot_ms"] = 120
    changed = Persona.from_dict(data)
    assert original.content_hash() != changed.content_hash()


def test_missing_field_is_a_schema_error() -> None:
    with pytest.raises(PersonaSchemaError):
        Persona.from_dict({"id": "x", "version": 1})


def test_transitions_must_sum_to_one() -> None:
    data = json.loads(Path("persona/profiles/balanced.json").read_text())
    data["transitions"]["reading"]["idle"] = 0.9
    with pytest.raises(PersonaSchemaError):
        Persona.from_dict(data)


def test_unknown_source_rejected() -> None:
    data = json.loads(Path("persona/profiles/balanced.json").read_text())
    data["source"] = "magic"
    with pytest.raises(PersonaSchemaError):
        Persona.from_dict(data)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda d: d["states"][0].update(record_size=8), "min_record_size"),
        (lambda d: d["states"][0].update(record_size=40_000), "record_size"),
        (lambda d: d["states"][0].update(slot_ms=2), "min_slot_ms"),
        (lambda d: d["states"][1].update(slot_ms=900), "floor_slot_ms"),
        (lambda d: d["states"][0].update(record_size=16_384), "max_bytes_per_s"),
        (lambda d: d["states"][0]["burst"].update(slot_ms=1), "min_slot_ms"),
        (lambda d: d.update(floor_slot_ms=5000), "floor"),
    ],
)
def test_limit_violations_fall_back_to_balanced(
    limits: Limits, mutate: Callable[[dict[str, Any]], None], expected: str
) -> None:
    data = json.loads(Path("persona/profiles/balanced.json").read_text())
    mutate(data)
    outcome = validate_dict(data, limits)
    assert outcome.fell_back is True
    assert outcome.persona.id == "balanced"
    assert any(expected in error for error in outcome.errors), outcome.errors


def test_invalid_file_falls_back_and_strict_load_raises(limits: Limits, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"id": "bad", "version": 1}')
    outcome = validate_file(bad, limits)
    assert outcome.fell_back
    assert outcome.persona.id == "balanced"
    with pytest.raises(PersonaValidationError):
        load_persona(bad, limits)


def test_id_must_match_file_name(limits: Limits, tmp_path: Path) -> None:
    data = json.loads(Path("persona/profiles/balanced.json").read_text())
    data["id"] = "other"
    path = tmp_path / "balanced.json"
    path.write_text(json.dumps(data))
    outcome = validate_file(path, limits)
    assert outcome.fell_back
    assert any("does not match" in e for e in outcome.errors)


def test_unknown_persona_id_raises(limits: Limits) -> None:
    with pytest.raises(PersonaValidationError):
        load_by_id("does-not-exist", "persona/profiles", limits)


def test_fallback_persona_is_valid(limits: Limits) -> None:
    persona = fallback_persona(limits)
    assert persona.id == "balanced"
    assert persona.content_hash() == builtin_balanced().content_hash()


def test_discovery_reports_invalid_personas_without_listing_them_as_valid(
    limits: Limits, tmp_path: Path
) -> None:
    good = tmp_path / "low.json"
    good.write_text(Path("persona/profiles/low.json").read_text())
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    rows = discover_personas(tmp_path, limits)
    by_path = {Path(r.path).name: r for r in rows}
    assert by_path["low.json"].valid is True
    assert by_path["broken.json"].valid is False
    valid_ids = {r.id for r in validated_personas(tmp_path, limits)}
    assert valid_ids == {"low"}
