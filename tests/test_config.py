"""Configuration reader tests (SDD 5.8)."""

from __future__ import annotations

import pytest

from config import Config, ConfigError, load_config, parse_yaml


def test_repository_config_loads_and_has_contract_keys() -> None:
    cfg = load_config()
    assert cfg.get_str("tor.socks_port", "") == 9150 or cfg.get_int("tor.socks_port") == 9150
    assert cfg.get_int("cover.floor_slot_ms") == 500
    assert cfg.get_int("cover.min_slot_ms") == 10
    assert cfg.get_int("cover.retry_s") == 10
    assert cfg.get_int("cover.greeting_record_size") == 1024
    assert cfg.get_str("tor.mode") in {"auto", "external", "simulated"}
    assert cfg.get_str("persona.default") == "balanced"
    assert isinstance(cfg.get("experiments.sites"), list)
    assert len(cfg.get("experiments.sites")) >= 10
    assert cfg.get_int("analysis.window_s") == 1


def test_dotted_access_and_defaults() -> None:
    cfg = Config({"a": {"b": {"c": 3}}, "flag": True, "name": "uqab"})
    assert cfg.get("a.b.c") == 3
    assert cfg.get("a.b.missing", "fallback") == "fallback"
    assert cfg.get_bool("flag") is True
    assert cfg.get_int("a.b.c") == 3
    with pytest.raises(ConfigError):
        cfg.require("nope.nothing")


def test_paths_resolve_against_repository_root() -> None:
    cfg = load_config()
    assert cfg.path("tls.ca").name == "ca.crt"
    assert cfg.path("paths.sqlite").name == "uqab.sqlite"


def test_overrides_are_deep_copies() -> None:
    cfg = load_config()
    other = cfg.with_overrides(**{"cover.floor_slot_ms": 250})
    assert other.get_int("cover.floor_slot_ms") == 250
    assert cfg.get_int("cover.floor_slot_ms") == 500


def test_yaml_subset_comments_quotes_and_lists() -> None:
    doc = parse_yaml(
        """
        # leading comment
        name: uqab  # trailing comment
        quoted: "a: b # not a comment"
        number: 42
        ratio: 0.25
        enabled: true
        empty:
        items:
          - one
          - two
        nested:
          deep:
            value: 7
        """
    )
    assert doc["name"] == "uqab"
    assert doc["quoted"] == "a: b # not a comment"
    assert doc["number"] == 42
    assert doc["ratio"] == 0.25
    assert doc["enabled"] is True
    assert doc["items"] == ["one", "two"]
    assert doc["nested"]["deep"]["value"] == 7


def test_bad_document_raises() -> None:
    with pytest.raises(ConfigError):
        parse_yaml("nested:\n    value: 1\n  bad: 2\n")
