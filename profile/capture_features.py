"""Opt-in clearnet capture importer (FR-16, FR-17, UC-3).

Reads a capture the user supplies (pcap from Wireshark/tcpdump, or a CSV of
timestamps and sizes), extracts aggregate features, and deletes the raw capture.
Refuses to run unless the consent screen has set the opt-in flag.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Iterable
from pathlib import Path

from analysis.features import FeatureWindow, extract_windows
from analysis.pcap import Packet, read_pcap
from app_api.consent import ConsentStore


class OptInRequired(PermissionError):
    """Clearnet learning was not enabled by the user (FR-16)."""


CSV_FIELDS = ("ts", "size")


def _read_csv(path: Path) -> list[Packet]:
    packets: list[Packet] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                ts = float(row["ts"])
                size = int(float(row["size"]))
            except (KeyError, TypeError, ValueError):
                continue
            direction = str(row.get("direction", "")).strip().lower()
            sport = int(float(row.get("sport", 0) or 0))
            dport = int(float(row.get("dport", 0) or 0))
            src = str(row.get("src", "client")) or "client"
            dst = str(row.get("dst", "server")) or "server"
            packets.append(
                Packet(
                    ts=ts,
                    src=dst if direction == "down" else src,
                    dst=src if direction == "down" else dst,
                    sport=dport if direction == "down" else sport,
                    dport=sport if direction == "down" else dport,
                    length=max(0, size),
                )
            )
    return packets


def read_capture(path: Path | str) -> list[Packet]:
    """Read a pcap file or a CSV of ``ts,size[,direction,src,dst,sport,dport]``."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    with source.open("rb") as handle:
        magic = handle.read(4)
    if magic in {b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"}:
        return list(read_pcap(source))
    return _read_csv(source)


def secure_delete(path: Path | str) -> None:
    """Overwrite the capture before unlinking it (FR-17)."""
    target = Path(path)
    try:
        size = target.stat().st_size
        with target.open("r+b") as handle:
            handle.seek(0)
            remaining = size
            block = b"\x00" * 65536
            while remaining > 0:
                chunk = min(remaining, len(block))
                handle.write(block[:chunk])
                remaining -= chunk
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass
    try:
        target.unlink()
    except FileNotFoundError:
        pass


def extract_features(
    capture_path: Path | str,
    *,
    flag_path: Path | str,
    window_s: float = 1.0,
    bridge_port: int | None = None,
    delete_raw: bool = True,
) -> list[FeatureWindow]:
    """Extract aggregate windows from a capture, deleting the raw file after."""
    store = ConsentStore(Path(flag_path), Path(flag_path).parent / "profile.json")
    if not store.is_opted_in():
        raise OptInRequired(
            "clearnet learning is off: the consent screen must be accepted in the app first"
        )
    packets = read_capture(capture_path)
    windows = extract_windows(packets, window_s=window_s, bridge_port=bridge_port)
    if delete_raw:
        secure_delete(capture_path)
    return windows


def summarise_periods(windows: Iterable[FeatureWindow]) -> list[dict[str, float]]:
    """Reduce windows to the per-second aggregate rows the learner consumes."""
    return [
        {
            "bytes_per_s": float(window.bytes),
            "mean_packet_size": float(window.mean_size),
            "mean_gap_ms": float(window.mean_gap_ms),
            "packets": float(window.packets),
        }
        for window in windows
    ]
