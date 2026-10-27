"""Automated privacy check for the clearnet profile (NFR-06, FR-17).

The profile may contain only aggregates.  This module both *checks* a finished
profile and is used as a unit-test oracle: any URL, host name, IP address, email
or free-text field is a finding.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ALLOWED_TOP_LEVEL = {
    "version",
    "k",
    "states",
    "transition_matrix",
    "size_histogram",
    "gap_histogram",
    "source",
    "windows",
}

ALLOWED_STATE_KEYS = {"id", "bytes_per_s", "mean_packet_size", "mean_gap_ms"}

# Fixed vocabulary values carry no user data, so they are allowed verbatim.
ALLOWED_LITERALS = {"source": {"clearnet", "static", "llm"}}

FORBIDDEN_PATTERNS = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\bwww\.", re.IGNORECASE),
    re.compile(r"\b[a-z0-9-]+\.(?:com|net|org|io|pk|edu|gov|co)\b", re.IGNORECASE),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    re.compile(r"/[a-z0-9_-]+/[a-z0-9_-]+", re.IGNORECASE),
)


def _walk(value: Any, path: str, findings: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                findings.append(f"{path}: non-string key {key!r}")
                continue
            _walk(child, f"{path}.{key}" if path else key, findings)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, f"{path}[{index}]", findings)
        return
    if isinstance(value, str):
        allowed = ALLOWED_LITERALS.get(path)
        if allowed is not None and value in allowed:
            return
        findings.append(f"{path}: free-text string {value!r} is not allowed")
        return
    if isinstance(value, bool) or value is None:
        findings.append(f"{path}: unexpected {value!r}")
        return
    if not isinstance(value, (int, float)):
        findings.append(f"{path}: unexpected type {type(value).__name__}")


def check_profile(profile: dict[str, Any]) -> list[str]:
    """Return every privacy or schema problem in a parsed profile."""
    findings: list[str] = []
    unknown = set(profile) - ALLOWED_TOP_LEVEL
    if unknown:
        findings.append(f"unexpected top-level key(s): {sorted(unknown)}")
    _walk(profile, "", findings)
    for index, state in enumerate(profile.get("states", []) or []):
        if not isinstance(state, dict):
            findings.append(f"states[{index}] is not an object")
            continue
        extra = set(state) - ALLOWED_STATE_KEYS
        if extra:
            findings.append(f"states[{index}] has unexpected key(s): {sorted(extra)}")
    matrix = profile.get("transition_matrix") or []
    for row_index, row in enumerate(matrix):
        if not isinstance(row, list) or not row:
            findings.append(f"transition_matrix[{row_index}] is not a list")
            continue
        total = sum(float(v) for v in row if isinstance(v, (int, float)))
        if abs(total - 1.0) > 1e-6:
            findings.append(f"transition_matrix[{row_index}] sums to {total:.6f}")
    return findings


def check_profile_file(path: Path | str) -> list[str]:
    target = Path(path)
    findings: list[str] = []
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return [f"cannot read {target}: {exc}"]
    text = raw.decode("utf-8", "replace")
    for pattern in FORBIDDEN_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(f"forbidden pattern in file: {match.group(0)!r}")
    try:
        profile = json.loads(text)
    except ValueError as exc:
        return [*findings, f"profile is not valid JSON: {exc}"]
    if not isinstance(profile, dict):
        return [*findings, "profile root must be an object"]
    findings.extend(check_profile(profile))
    return findings


def assert_private(path: Path | str) -> None:
    findings = check_profile_file(path)
    if findings:
        raise AssertionError("profile privacy check failed: " + "; ".join(findings))
