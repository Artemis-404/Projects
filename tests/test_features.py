"""Window labelling and feature extraction (FR-21, FR-22, D19)."""

from __future__ import annotations

from analysis.features import GroundTruthWindow, label_for


def test_boundary_windows_are_dropped_not_guessed() -> None:
    """A window that straddles ACTIVE and IDLE gets no label (D19)."""
    truth = [
        GroundTruthWindow(start=0.0, end=1.0, label="active"),
        GroundTruthWindow(start=1.0, end=3.0, label="idle"),
    ]
    assert label_for(0.0, 1.0, truth) == "active"
    assert label_for(1.0, 2.0, truth) == "idle"
    # 0.5 s of each: neither label is honest.
    assert label_for(0.6, 1.6, truth) == ""
    # 85 % IDLE inside a window: good enough.
    assert label_for(0.9, 1.9, truth) == "idle"
    assert label_for(0.15, 1.15, truth) == "active"
