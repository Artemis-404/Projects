"""Clearnet profile pipeline tests (FR-16 to FR-19, NFR-06)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from analysis.features import (
    FeatureWindow,
    extract_windows,
    read_ground_truth,
    write_features,
    read_features,
    write_ground_truth,
    GroundTruthWindow,
)
from analysis.pcap import build_packet, read_pcap, write_pcap
from app_api.consent import CONSENT_TEXT_VERSION, ConsentStore
from config import load_config
from persona.validator import Limits, validate_dict
from profile.capture_features import OptInRequired, extract_features, read_capture
from profile.learner import ProfileError, learn, load_profile, write_profile
from profile.privacy import check_profile, check_profile_file
from profile.sampler import sample_persona, sample_variants, write_personas

BRIDGE_PORT = 9443


def synthetic_packets(duration_s: int = 60, seed: int = 3) -> list[tuple[float, bytes]]:
    """ACTIVE for 2 s, IDLE for 4 s, repeating, with distinct traffic shapes."""
    rng = random.Random(seed)
    frames: list[tuple[float, bytes]] = []
    t = 1000.0
    while t < 1000.0 + duration_s:
        phase = int(t - 1000.0) % 6
        if phase < 2:  # active: many small packets both ways
            for _ in range(rng.randint(12, 20)):
                up = rng.random() < 0.5
                size = rng.randint(60, 900)
                frames.append(
                    (
                        t,
                        build_packet(
                            "10.0.0.2" if up else "10.0.0.9",
                            "10.0.0.9" if up else "10.0.0.2",
                            rng.randint(40000, 50000) if up else BRIDGE_PORT,
                            BRIDGE_PORT if up else rng.randint(40000, 50000),
                            0,
                        )
                        if size < 100
                        else build_packet(
                            "10.0.0.2" if up else "10.0.0.9",
                            "10.0.0.9" if up else "10.0.0.2",
                            rng.randint(40000, 50000) if up else BRIDGE_PORT,
                            BRIDGE_PORT if up else rng.randint(40000, 50000),
                            size - 54,
                        ),
                    )
                )
                t += rng.uniform(0.02, 0.08)
        else:  # idle: only the persona floor records
            for _ in range(3):
                frames.append(
                    (
                        t,
                        build_packet(
                            "10.0.0.2",
                            "10.0.0.9",
                            44000,
                            BRIDGE_PORT,
                            1024 - 54,
                        ),
                    )
                )
                t += 0.4
    return frames


@pytest.fixture(scope="module")
def limits() -> Limits:
    return Limits.from_config(load_config())


def test_pcap_roundtrip(tmp_path: Path) -> None:
    frames = synthetic_packets(20)
    path = tmp_path / "cap.pcap"
    count = write_pcap(path, frames)
    assert count == len(frames)
    packets = list(read_pcap(path))
    assert len(packets) == len(frames)
    assert packets[0].length > 0
    assert {p.dport for p in packets} & {BRIDGE_PORT}


def test_feature_windows_have_all_columns_and_labels(tmp_path: Path) -> None:
    packets = list(read_pcap(write_and_return(tmp_path, synthetic_packets(40))))
    truth = [
        GroundTruthWindow(1000.0, 1002.0, "active"),
        GroundTruthWindow(1002.0, 1006.0, "idle"),
        GroundTruthWindow(1006.0, 1008.0, "active"),
        GroundTruthWindow(1008.0, 1012.0, "idle"),
    ]
    windows = extract_windows(packets, window_s=1.0, bridge_port=BRIDGE_PORT, ground_truth=truth)
    assert len(windows) >= 30
    labels = {w.label for w in windows}
    assert "active" in labels and "idle" in labels
    active_bytes = [w.bytes for w in windows if w.label == "active"]
    idle_bytes = [w.bytes for w in windows if w.label == "idle"]
    assert sum(active_bytes) / len(active_bytes) > sum(idle_bytes) / len(idle_bytes) / 2
    for window in windows:
        assert 0.0 <= window.up_ratio <= 1.0
        assert window.mean_size >= 0

    features_csv = tmp_path / "features.csv"
    truth_csv = tmp_path / "truth.csv"
    write_features(features_csv, windows)
    write_ground_truth(truth_csv, truth)
    again = read_features(features_csv)
    assert len(again) == len(windows)
    assert read_ground_truth(truth_csv)[0].label == "active"


def write_and_return(tmp_path: Path, frames: list[tuple[float, bytes]]) -> Path:
    path = tmp_path / "capture.pcap"
    write_pcap(path, frames)
    return path


def test_importer_refuses_without_opt_in(tmp_path: Path) -> None:
    capture = write_and_return(tmp_path, synthetic_packets(10))
    flag = tmp_path / "profile" / "opt_in.json"
    with pytest.raises(OptInRequired):
        extract_features(capture, flag_path=flag)
    assert capture.exists(), "the raw capture must not be deleted on refusal"


def test_importer_deletes_raw_capture_after_extraction(tmp_path: Path) -> None:
    capture = write_and_return(tmp_path, synthetic_packets(30))
    flag = tmp_path / "profile" / "opt_in.json"
    store = ConsentStore(flag, tmp_path / "profile" / "profile.json")
    store.accept(CONSENT_TEXT_VERSION)
    windows = extract_features(capture, flag_path=flag, bridge_port=BRIDGE_PORT)
    assert windows
    assert not capture.exists(), "raw capture must be deleted (FR-17)"


def test_csv_input_is_supported(tmp_path: Path) -> None:
    csv_path = tmp_path / "browsing.csv"
    rows = ["ts,size,direction"]
    for i in range(120):
        rows.append(f"{500.0 + i * 0.1},{200 if i % 3 else 1400},up")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    packets = read_capture(csv_path)
    assert len(packets) == 120
    windows = extract_windows(packets, window_s=1.0)
    assert windows
    assert sum(w.bytes for w in windows) > 0


def test_learner_produces_private_profile(tmp_path: Path) -> None:
    packets = list(read_pcap(write_and_return(tmp_path, synthetic_packets(90))))
    windows = extract_windows(packets, window_s=1.0, bridge_port=BRIDGE_PORT)
    profile = learn(windows, k=4)
    assert profile["version"] == 1
    assert profile["k"] == 4
    assert len(profile["states"]) == 4
    assert len(profile["transition_matrix"]) == 4
    for row in profile["transition_matrix"]:
        assert abs(sum(row) - 1.0) < 1e-6
    assert profile["size_histogram"]["counts"]
    assert profile["gap_histogram"]["counts"]
    assert check_profile(profile) == []

    path = tmp_path / "profile.json"
    write_profile(path, profile)
    assert check_profile_file(path) == []
    loaded = load_profile(path)
    assert loaded["k"] == 4


def test_privacy_check_catches_leaks(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "k": 2,
        "states": [{"id": 0, "bytes_per_s": 1.0, "mean_packet_size": 2.0, "mean_gap_ms": 3.0}],
        "transition_matrix": [[1.0, 0.0], [0.0, 1.0]],
        "size_histogram": {"edges": [0, 1], "counts": [1]},
        "gap_histogram": {"edges": [0, 1], "counts": [1]},
        "note": "user visited https://example.com/login today",
    }
    findings = check_profile(bad)
    assert any("unexpected top-level" in f for f in findings)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    file_findings = check_profile_file(path)
    assert any("forbidden pattern" in f for f in file_findings)
    assert any("free-text" in f for f in file_findings)


def test_learner_rejects_empty_input() -> None:
    with pytest.raises(ProfileError):
        learn([])


def test_sampled_personas_pass_the_validator(tmp_path: Path, limits: Limits) -> None:
    packets = list(read_pcap(write_and_return(tmp_path, synthetic_packets(60))))
    windows = extract_windows(packets, window_s=1.0, bridge_port=BRIDGE_PORT)
    profile = learn(windows, k=3)
    persona = sample_persona(profile, limits, persona_id="clearnet-a")
    assert persona.source == "clearnet"
    outcome = validate_dict(persona.to_dict(), limits)
    assert outcome.ok, outcome.errors

    variants = sample_variants(profile, limits, count=2)
    assert len(variants) == 2
    assert variants[1].id.endswith("-2")
    paths = write_personas(variants, tmp_path / "generated")
    assert all(p.is_file() for p in paths)


def test_sampled_persona_respects_limits(limits: Limits) -> None:
    profile = {
        "version": 1,
        "k": 2,
        "states": [
            {"id": 0, "bytes_per_s": 250_000.0, "mean_packet_size": 1200.0, "mean_gap_ms": 5.0},
            {"id": 1, "bytes_per_s": 10.0, "mean_packet_size": 80.0, "mean_gap_ms": 900.0},
        ],
        "transition_matrix": [[0.5, 0.5], [0.25, 0.75]],
        "size_histogram": {"edges": [0, 1000], "counts": [5]},
        "gap_histogram": {"edges": [0, 500], "counts": [5]},
    }
    persona = sample_persona(profile, limits, persona_id="clearnet-limits")
    for state in persona.states:
        assert limits.min_record_size <= state.record_size <= 16_384
        assert state.slot_ms >= limits.min_slot_ms
        assert state.slot_ms <= persona.floor_slot_ms
        assert state.record_size * 1000 / state.slot_ms <= limits.max_bytes_per_s
    for row in persona.transitions.values():
        assert abs(sum(row.values()) - 1.0) < 1e-9


def test_end_to_end_capture_to_persona(tmp_path: Path, limits: Limits) -> None:
    capture = write_and_return(tmp_path, synthetic_packets(60))
    flag = tmp_path / "profile" / "opt_in.json"
    ConsentStore(flag, tmp_path / "profile" / "profile.json").accept(CONSENT_TEXT_VERSION)
    windows = extract_features(capture, flag_path=flag, bridge_port=BRIDGE_PORT)
    assert not capture.exists()
    profile = learn(windows, k=4)
    profile_path = tmp_path / "profile" / "profile.json"
    write_profile(profile_path, profile)
    assert check_profile_file(profile_path) == []
    personas = sample_variants(profile, limits, count=2)
    assert all(validate_dict(p.to_dict(), limits).ok for p in personas)
    assert all(p.source == "clearnet" for p in personas)
