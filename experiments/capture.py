"""Slot-log to capture conversion for lab experiments (FR-20, FR-21).

The lab has no packet capture (no tcpdump, no root), and it does not need one:
every shaped record the client and server write crosses exactly one link, and
``SlotRecorder`` already logs ``(t, size, state)`` for each of them.  This module
replays those logs into the same ``Packet`` stream a pcap reader would produce,
so the feature extractor and classifiers are exercised on identical code paths
for lab and real captures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from analysis.pcap import Packet, write_pcap

BRIDGE_PORT = 9443
CLIENT_IP = "10.0.0.2"
BRIDGE_IP = "10.0.0.9"


@dataclass(frozen=True, slots=True)
class SlotRecord:
    t: float
    index: int
    size: int
    state: str = ""
    burst: bool = False
    error_ms: float = 0.0


def read_slot_log(path: Path | str, *, offset: int = 0) -> tuple[list[SlotRecord], int]:
    """Read slot records appended after byte ``offset``.

    Returns the records and the new file offset, so a caller can capture only the
    records produced during one experiment run.
    """
    target = Path(path)
    if not target.is_file():
        return [], offset
    records: list[SlotRecord] = []
    with target.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read()
        new_offset = handle.tell()
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        try:
            records.append(
                SlotRecord(
                    t=float(row["t"]),
                    index=int(row.get("index", 0) or 0),
                    size=int(row.get("size", 0) or 0),
                    state=str(row.get("state", "")),
                    burst=bool(row.get("burst", False)),
                    error_ms=float(row.get("error_ms", 0.0) or 0.0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return records, new_offset


def file_offset(path: Path | str) -> int:
    """Current size of a log file, used as the ``since`` marker."""
    target = Path(path)
    try:
        return target.stat().st_size
    except OSError:
        return 0


def to_packets(
    records: list[SlotRecord],
    *,
    t0: float,
    base_ts: float,
    downlink: bool = False,
    bridge_port: int = BRIDGE_PORT,
) -> list[Packet]:
    """Convert slot records into packets anchored at ``base_ts``."""
    packets: list[Packet] = []
    for index, record in enumerate(records):
        if record.size <= 0:
            continue
        ts = base_ts + max(0.0, record.t - t0)
        sport = 40000 + (index % 200)
        if downlink:
            packets.append(
                Packet(
                    ts=ts,
                    src=BRIDGE_IP,
                    dst=CLIENT_IP,
                    sport=bridge_port,
                    dport=sport,
                    length=record.size,
                )
            )
        else:
            packets.append(
                Packet(
                    ts=ts,
                    src=CLIENT_IP,
                    dst=BRIDGE_IP,
                    sport=sport,
                    dport=bridge_port,
                    length=record.size,
                )
            )
    packets.sort(key=lambda p: p.ts)
    return packets


def capture_from_logs(
    *,
    client_log: Path | str,
    server_log: Path | str,
    client_offset: int,
    server_offset: int,
    t0: float,
    base_ts: float,
    bridge_port: int = BRIDGE_PORT,
) -> list[Packet]:
    """Build one combined packet list from both slot logs."""
    uplink, _ = read_slot_log(client_log, offset=client_offset)
    downlink, _ = read_slot_log(server_log, offset=server_offset)
    packets = to_packets(uplink, t0=t0, base_ts=base_ts, bridge_port=bridge_port)
    packets.extend(
        to_packets(downlink, t0=t0, base_ts=base_ts, downlink=True, bridge_port=bridge_port)
    )
    packets.sort(key=lambda p: p.ts)
    return packets


def write_capture(path: Path | str, packets: list[Packet]) -> int:
    """Serialize packets to a pcap file (timestamps preserved)."""
    return write_pcap(path, [(packet.ts, frame) for packet, frame in _frames(packets)])


def _frames(packets: list[Packet]) -> list[tuple[Packet, bytes]]:
    from analysis.pcap import build_packet

    out: list[tuple[Packet, bytes]] = []
    for index, packet in enumerate(packets):
        payload = max(0, packet.length - 54)
        frame = build_packet(
            packet.src,
            packet.dst,
            packet.sport,
            packet.dport,
            payload,
            seq=index * 1000,
        )
        out.append((packet, frame))
    return out


def slots_from_file(path: Path | str) -> list[SlotRecord]:
    records, _ = read_slot_log(path)
    return records


@dataclass(frozen=True, slots=True)
class PlainRecord:
    t: float
    index: int
    size: int
    direction: str


def read_plain_log(path: Path | str, *, offset: int = 0) -> tuple[list[PlainRecord], int]:
    """Read the plain-Tor byte log (configuration A) appended after ``offset``."""
    target = Path(path)
    if not target.is_file():
        return [], offset
    records: list[PlainRecord] = []
    with target.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read()
        new_offset = handle.tell()
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
            records.append(
                PlainRecord(
                    t=float(row["t"]),
                    index=int(row.get("index", 0) or 0),
                    size=int(row.get("size", 0) or 0),
                    direction=str(row.get("dir", "up")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return records, new_offset


def plain_to_packets(
    records: list[PlainRecord], *, t0: float, base_ts: float, bridge_port: int = BRIDGE_PORT
) -> list[Packet]:
    """Convert plain-Tor byte records into packets."""
    packets: list[Packet] = []
    for index, record in enumerate(records):
        if record.size <= 0:
            continue
        ts = base_ts + max(0.0, record.t - t0)
        down = record.direction == "down"
        packets.append(
            Packet(
                ts=ts,
                src=BRIDGE_IP if down else CLIENT_IP,
                dst=CLIENT_IP if down else BRIDGE_IP,
                sport=bridge_port if down else 40000 + (index % 200),
                dport=40000 + (index % 200) if down else bridge_port,
                length=record.size,
            )
        )
    packets.sort(key=lambda p: p.ts)
    return packets


def capture_from_plain_log(
    *,
    plain_log: Path | str,
    offset: int,
    t0: float,
    base_ts: float,
    bridge_port: int = BRIDGE_PORT,
) -> list[Packet]:
    records, _ = read_plain_log(plain_log, offset=offset)
    return plain_to_packets(records, t0=t0, base_ts=base_ts, bridge_port=bridge_port)
