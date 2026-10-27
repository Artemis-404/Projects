"""Minimal pcap reader/writer (FR-21, FR-22).

Only what UQAB needs: classic libpcap files with Ethernet or raw-IP link types,
IPv4/IPv6, TCP or UDP.  Writing pcap lets the lab capture backend record the
client-to-bridge link without root or tcpdump, while ``read_pcap`` consumes both
those files and captures produced by tcpdump on the lab machine.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

PCAP_MAGIC_LE = 0xA1B2C3D4
PCAP_MAGIC_BE = 0xD4C3B2A1
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101

ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_IPV6 = 0x86DD
IPPROTO_TCP = 6
IPPROTO_UDP = 17


@dataclass(frozen=True, slots=True)
class Packet:
    """One captured packet, reduced to what the classifier needs."""

    ts: float
    src: str
    dst: str
    sport: int
    dport: int
    length: int
    protocol: int = IPPROTO_TCP

    @property
    def size(self) -> int:
        return self.length


def _cheap_checksum(data: bytes) -> int:
    total = 0
    for index in range(0, len(data) - 1, 2):
        total += (data[index] << 8) | data[index + 1]
    if len(data) % 2:
        total += data[-1] << 8
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _ip_bytes(address: str) -> bytes:
    if ":" in address:  # pragma: no cover - IPv6 writing is not needed for UQAB
        raise ValueError("writing IPv6 packets is not supported")
    return bytes(int(part) for part in address.split("."))


def build_packet(
    src: str,
    dst: str,
    sport: int,
    dport: int,
    payload_size: int = 0,
    *,
    protocol: int = IPPROTO_TCP,
    seq: int = 1,
) -> bytes:
    """Build one Ethernet/IPv4/TCP frame with a given on-wire size."""
    payload = bytes(min(max(payload_size, 0), 1400))
    tcp_header = struct.pack(
        "!HHIIHHHH",
        sport,
        dport,
        seq,
        0,
        5 << 12,
        0x0018,  # PSH + ACK
        8192,
        0,
    )
    total_len = 20 + len(tcp_header) + len(payload)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_len,
        0x1234,
        0x4000,
        64,
        protocol,
        0,
        _ip_bytes(src),
        _ip_bytes(dst),
    )
    checksum = _cheap_checksum(ip_header)
    ip_header = ip_header[:10] + struct.pack("!H", checksum) + ip_header[12:]
    ethernet = b"\x02\x00\x00\x00\x00\x01\x02\x00\x00\x00\x00\x02\x08\x00"
    frame = ethernet + ip_header + tcp_header + payload
    # Pad to the requested size with TCP payload bytes only (never below headers).
    target = 14 + total_len
    if len(frame) < target:  # pragma: no cover - defensive
        frame += b"\x00" * (target - len(frame))
    if target < 54:  # pragma: no cover
        frame += b"\x00" * (54 - target)
    return frame


def write_pcap(path: Path | str, packets: Iterable[tuple[float, bytes]]) -> int:
    """Write ``(timestamp, frame)`` pairs as a classic pcap file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with target.open("wb") as handle:
        handle.write(
            struct.pack(
                "<IHHiIII", PCAP_MAGIC_LE, 2, 4, 0, 0, 65535, LINKTYPE_ETHERNET
            )
        )
        for ts, frame in packets:
            seconds = int(ts)
            micros = int(round((ts - seconds) * 1_000_000))
            if micros >= 1_000_000:
                seconds += 1
                micros -= 1_000_000
            handle.write(struct.pack("<IIII", seconds, micros, len(frame), len(frame)))
            handle.write(frame)
            count += 1
    return count


def _parse_ipv4(data: bytes) -> tuple[str, str, int, int, int] | None:
    if len(data) < 20 or (data[0] >> 4) != 4:
        return None
    ihl = (data[0] & 0x0F) * 4
    if len(data) < ihl + 8:
        return None
    total_len = int.from_bytes(data[2:4], "big")
    protocol = data[9]
    src = ".".join(str(b) for b in data[12:16])
    dst = ".".join(str(b) for b in data[16:20])
    transport = data[ihl:]
    if len(transport) < 4:
        return None
    sport = int.from_bytes(transport[0:2], "big")
    dport = int.from_bytes(transport[2:4], "big")
    return src, dst, sport, dport, total_len if total_len >= 20 else len(data)


def _parse_ipv6(data: bytes) -> tuple[str, str, int, int, int] | None:
    if len(data) < 40 or (data[0] >> 4) != 6:
        return None
    payload_len = int.from_bytes(data[4:6], "big")
    next_header = data[6]
    src = ":".join(
        f"{int.from_bytes(data[i:i + 2], 'big'):x}" for i in range(8, 24, 2)
    )
    dst = ":".join(
        f"{int.from_bytes(data[i:i + 2], 'big'):x}" for i in range(24, 40, 2)
    )
    transport = data[40:]
    if len(transport) < 4:
        return None
    sport = int.from_bytes(transport[0:2], "big")
    dport = int.from_bytes(transport[2:4], "big")
    return src, dst, sport, dport, payload_len + 40


def read_pcap(path: Path | str) -> Iterator[Packet]:
    """Yield packets from a pcap file written by tcpdump or by this module."""
    source = Path(path)
    raw = source.read_bytes()
    if len(raw) < 24:
        return
    magic = struct.unpack("<I", raw[:4])[0]
    if magic == PCAP_MAGIC_LE:
        endian = "<"
    elif magic == PCAP_MAGIC_BE:
        endian = ">"
        magic = struct.unpack(">I", raw[:4])[0]
    else:
        raise ValueError(f"{source} is not a classic pcap file")
    linktype = struct.unpack(f"{endian}I", raw[20:24])[0]
    offset = 24
    while offset + 16 <= len(raw):
        ts_sec, ts_usec, incl_len, _orig = struct.unpack(
            f"{endian}IIII", raw[offset : offset + 16]
        )
        offset += 16
        frame = raw[offset : offset + incl_len]
        offset += incl_len
        if len(frame) < incl_len:
            break
        ts = ts_sec + ts_usec / 1_000_000
        if linktype == LINKTYPE_ETHERNET:
            if len(frame) < 14:
                continue
            ethertype = int.from_bytes(frame[12:14], "big")
            payload = frame[14:]
        elif linktype == LINKTYPE_RAW:
            ethertype = ETHERTYPE_IPV4 if (frame[0] >> 4) == 4 else ETHERTYPE_IPV6
            payload = frame
        else:  # pragma: no cover - unsupported link type
            continue
        if ethertype == ETHERTYPE_IPV4:
            parsed = _parse_ipv4(payload)
        elif ethertype == ETHERTYPE_IPV6:
            parsed = _parse_ipv6(payload)
        else:
            continue
        if parsed is None:
            continue
        src, dst, sport, dport, length = parsed
        yield Packet(
            ts=ts, src=src, dst=dst, sport=sport, dport=dport, length=length
        )


def pcap_duration(path: Path | str) -> float:
    packets = list(read_pcap(path))
    if not packets:
        return 0.0
    return packets[-1].ts - packets[0].ts
