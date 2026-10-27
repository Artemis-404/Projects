"""Scheduler and shaped-channel tests (FR-06, FR-08, FR-13, NFR-03, NFR-04)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cover.record import build_record, extract_data, parse_record
from cover.scheduler import ByteQueue, Counters, ShapedChannel, SlotRecorder
from cover.timetable import Timetable
from persona.model import Persona, builtin_balanced
from protocol.frames import HEADER_SIZE, Frame, FrameType, ping_payload
from tests.conftest import run
from tests.support import FakeReader, FakeWriter


def persona_with(slot_ms: int = 20, record_size: int = 512, floor_slot_ms: int = 500) -> Persona:
    return Persona.from_dict(
        {
            "id": "testpersona",
            "version": 1,
            "source": "static",
            "floor_slot_ms": floor_slot_ms,
            "start_state": "run",
            "states": [
                {
                    "name": "run",
                    "record_size": record_size,
                    "slot_ms": slot_ms,
                    "duration_s": [3600, 3600],
                }
            ],
            "transitions": {"run": {"run": 1.0}},
        }
    )


def test_counters_overhead_and_snapshot() -> None:
    counters = Counters()
    counters.add_up(100, 412)
    counters.add_down(300, 200)
    assert counters.real_total == 400
    assert counters.total == 1012
    assert counters.overhead == pytest.approx(2.53, abs=0.01)
    snap = counters.snapshot()
    assert snap["real_up"] == 100 and snap["cover_down"] == 200
    counters.reset()
    assert counters.overhead == 0.0


def test_byte_queue_backpressure_and_take() -> None:
    async def scenario() -> None:
        queue = ByteQueue(max_bytes=1024)
        await queue.put(b"a" * 700)
        await queue.put(b"b" * 300)
        assert len(queue) == 1000
        assert queue.take(400) == b"a" * 400
        assert queue.take(10_000) == b"a" * 300 + b"b" * 300
        assert len(queue) == 0
        queue.close()
        await queue.put(b"ignored")
        assert len(queue) == 0

    run(scenario())


def test_uplink_sends_exactly_one_record_per_slot() -> None:
    persona = persona_with(slot_ms=20, record_size=256)
    counters = Counters()
    writer = FakeWriter()
    queue = ByteQueue()

    async def scenario() -> None:
        channel = ShapedChannel(
            FakeReader(),  # type: ignore[arg-type]
            writer,  # type: ignore[arg-type]
            Timetable(persona, 99),
            counters,
            queue,
            on_data=_noop,
        )
        task = asyncio.create_task(channel.uplink())
        await asyncio.sleep(0.32)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    # ~15 slots in 320 ms at 20 ms; exactly one record per slot, never larger.
    assert 10 <= len(writer.writes) <= 18
    assert {len(w) for w in writer.writes} == {256}
    for raw in writer.writes:
        parse_record(raw, 256)
    assert counters.real_up == 0  # nothing queued, so every record is padding


def test_real_data_never_adds_or_enlarges_records() -> None:
    persona = persona_with(slot_ms=20, record_size=256)
    counters = Counters()
    writer = FakeWriter()
    queue = ByteQueue()

    async def scenario() -> None:
        await queue.put(b"Z" * 5000)
        channel = ShapedChannel(
            FakeReader(),  # type: ignore[arg-type]
            writer,  # type: ignore[arg-type]
            Timetable(persona, 5),
            counters,
            queue,
            on_data=_noop,
        )
        task = asyncio.create_task(channel.uplink())
        await asyncio.sleep(0.22)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert {len(w) for w in writer.writes} == {256}
    # 253 payload bytes per record at most (256 - 3 header bytes).
    assert counters.real_up <= len(writer.writes) * (256 - HEADER_SIZE)
    assert counters.real_up > 0
    # Whatever is sent is exactly a prefix of what was queued, in order.
    recovered = b"".join(
        extract_data(parse_record(raw, 256)) for raw in writer.writes
    )
    assert recovered == b"Z" * len(recovered)
    assert len(recovered) == counters.real_up


def test_floor_rate_is_enforced_even_for_slow_personas() -> None:
    persona = persona_with(slot_ms=400, record_size=256, floor_slot_ms=500)
    writer = FakeWriter()

    async def scenario() -> None:
        channel = ShapedChannel(
            FakeReader(),  # type: ignore[arg-type]
            writer,  # type: ignore[arg-type]
            Timetable(persona, 3),  # 400 ms slots are within the 500 ms floor
            Counters(),
            ByteQueue(),
            on_data=_noop,
            floor_slot_ms=40,  # a stricter floor: never slower than 40 ms
        )
        task = asyncio.create_task(channel.uplink())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    # With a 40 ms floor over 300 ms we expect roughly 7 records, not 1.
    assert len(writer.writes) >= 5


def test_slot_timing_error_stays_under_5ms_p95() -> None:
    persona = persona_with(slot_ms=25, record_size=256)
    recorder = SlotRecorder()

    async def scenario() -> None:
        channel = ShapedChannel(
            FakeReader(),  # type: ignore[arg-type]
            FakeWriter(),  # type: ignore[arg-type]
            Timetable(persona, 17),
            Counters(),
            ByteQueue(),
            on_data=_noop,
            recorder=recorder,
        )
        task = asyncio.create_task(channel.uplink())
        await asyncio.sleep(0.6)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    summary = recorder.summary()
    assert summary["n"] >= 15
    assert summary["p95_error_ms"] <= 5.0, summary
    assert summary["max_error_ms"] <= 50.0, summary


def test_downlink_recovers_data_and_handles_control_frames() -> None:
    persona = persona_with(slot_ms=10, record_size=128)
    received: list[bytes] = []
    pings: list[dict[str, object]] = []

    records = [
        build_record(b"", 128).raw,  # slot 0 is the greeting slot, always skipped
        build_record(b"hello", 128).raw,
        build_record(b"world!!", 128).raw,
        build_record(
            b"", 128, prefix=(Frame(FrameType.PING, ping_payload({"v": 1, "ok": True})),)
        ).raw,
    ]

    async def on_data(data: bytes) -> None:
        received.append(data)

    async def scenario() -> None:
        from security.guard import TunnelClosed

        channel = ShapedChannel(
            FakeReader(b"".join(records)),  # type: ignore[arg-type]
            FakeWriter(),  # type: ignore[arg-type]
            Timetable(persona, 1),
            Counters(),
            ByteQueue(),
            on_data=on_data,
            on_ping=pings.append,
        )
        # The peer stops sending when the stream ends, which must fail closed.
        with pytest.raises(TunnelClosed):
            await channel.downlink()

    run(scenario())
    assert b"".join(received) == b"helloworld!!"
    assert pings and pings[0]["ok"] is True


def test_downlink_close_frame_ends_session() -> None:
    from security.guard import TunnelClosed

    persona = persona_with(slot_ms=10, record_size=128)
    record = build_record(b"", 128).raw + build_record(
        b"", 128, prefix=(Frame(FrameType.CLOSE, b""),)
    ).raw

    async def scenario() -> None:
        channel = ShapedChannel(
            FakeReader(record),  # type: ignore[arg-type]
            FakeWriter(),  # type: ignore[arg-type]
            Timetable(persona, 1),
            Counters(),
            ByteQueue(),
            on_data=_noop,
        )
        await channel.downlink()

    with pytest.raises(TunnelClosed):
        run(scenario())


def test_short_read_is_reported_as_tunnel_closed() -> None:
    from security.guard import TunnelClosed

    persona = persona_with(slot_ms=10, record_size=128)

    async def scenario() -> None:
        channel = ShapedChannel(
            FakeReader(build_record(b"", 128).raw + b"\x02\x00\x00"),  # type: ignore[arg-type]
            FakeWriter(),  # type: ignore[arg-type]
            Timetable(persona, 1),
            Counters(),
            ByteQueue(),
            on_data=_noop,
        )
        await channel.downlink()

    with pytest.raises(TunnelClosed):
        run(scenario())


def test_slot_recorder_writes_json_lines(tmp_path: Path) -> None:
    persona = persona_with(slot_ms=10, record_size=64)
    path = tmp_path / "slots.jsonl"

    async def scenario() -> None:
        recorder = SlotRecorder(path)
        channel = ShapedChannel(
            FakeReader(),  # type: ignore[arg-type]
            FakeWriter(),  # type: ignore[arg-type]
            Timetable(persona, 2),
            Counters(),
            ByteQueue(),
            on_data=_noop,
            recorder=recorder,
        )
        task = asyncio.create_task(channel.uplink())
        await asyncio.sleep(0.12)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        recorder.close()

    run(scenario())
    lines = path.read_text().strip().splitlines()
    assert len(lines) >= 3
    assert '"size":64' in lines[0]
    assert '"error_ms"' in lines[0]


def test_persona_period_changes_are_reported() -> None:
    persona = builtin_balanced()
    periods: list[str] = []

    async def scenario() -> None:
        channel = ShapedChannel(
            FakeReader(),  # type: ignore[arg-type]
            FakeWriter(),  # type: ignore[arg-type]
            Timetable(persona, 11),
            Counters(),
            ByteQueue(),
            on_data=_noop,
            on_period=periods.append,
        )
        task = asyncio.create_task(channel.uplink())
        await asyncio.sleep(0.35)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    # The balanced persona starts in "reading"; any change must come from the
    # timetable, never from traffic (which was empty here).
    assert all(period in persona.state_names for period in periods)


async def _noop(_data: bytes) -> None:
    return None
