"""Replay determinism, torn-write recovery, snapshots and retention."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from matching_engine.domain import Command
from matching_engine.engine import MatchingEngine
from matching_engine.journal import Journal
from matching_engine.sequencer import Sequencer
from matching_engine.snapshot import encode_snapshot
from matching_engine.wal import WalCorruptionError
from matching_engine.workload import order_flow

FLOW = list(order_flow(seed=7, count=2_000, accounts=20, depth_ticks=10))


class Market:
    """The live side of a test: sequencer, journal and engine wired like the gateway."""

    def __init__(self, root: Path, *, keep_snapshots: int = 2) -> None:
        self.journal = Journal(root, fsync=False, keep_snapshots=keep_snapshots)
        self.engine, report = self.journal.recover()
        clock = itertools.count(report.last_ts + 1)
        self.sequencer = Sequencer(report.last_seq + 1, report.last_ts, clock.__next__)
        self.journal.open(self.sequencer.next_seq)

    def run(self, commands: list[Command], batch: int = 32) -> None:
        for i in range(0, len(commands), batch):
            stamped = [self.sequencer.stamp(c) for c in commands[i : i + batch]]
            for sc in stamped:
                self.journal.append(sc)
            self.journal.sync()
            for sc in stamped:
                self.engine.process(sc)

    def checkpoint(self) -> None:
        blob = encode_snapshot(self.engine, self.sequencer.last_ts)
        self.journal.checkpoint(blob, self.engine.last_seq)

    def close(self) -> None:
        self.journal.close()


def reference(commands: list[Command]) -> MatchingEngine:
    """The same flow applied in memory, with no persistence involved."""
    ref = MatchingEngine()
    sequencer = Sequencer(clock=itertools.count(1).__next__)
    for c in commands:
        ref.process(sequencer.stamp(c))
    return ref


def segment_files(root: Path) -> list[Path]:
    return sorted((root / "wal").glob("*.wal"))


def test_replaying_the_same_log_twice_gives_the_same_state(tmp_path: Path) -> None:
    live = Market(tmp_path)
    live.run(FLOW)
    live.close()

    first, _ = Journal(tmp_path, fsync=False).recover()
    second, report = Journal(tmp_path, fsync=False).recover()

    assert first.state_hash() == second.state_hash() == live.engine.state_hash()
    assert report.replayed == len(FLOW)
    assert report.torn is None


def test_a_truncated_last_record_is_dropped_and_the_log_repaired(tmp_path: Path) -> None:
    live = Market(tmp_path)
    live.run(FLOW)
    live.close()
    (segment,) = segment_files(tmp_path)
    size = segment.stat().st_size
    with segment.open("r+b") as f:
        f.truncate(size - 7)

    engine, report = Journal(tmp_path, fsync=False).recover()

    assert report.last_seq == len(FLOW) - 1
    assert report.torn is not None
    assert engine.state_hash() == reference(FLOW[:-1]).state_hash()
    assert segment.stat().st_size < size - 7, "torn bytes should be cut off"


def test_a_corrupted_last_record_is_dropped(tmp_path: Path) -> None:
    live = Market(tmp_path)
    live.run(FLOW)
    live.close()
    (segment,) = segment_files(tmp_path)
    data = bytearray(segment.read_bytes())
    data[-1] ^= 0xFF
    segment.write_bytes(bytes(data))

    engine, report = Journal(tmp_path, fsync=False).recover()

    assert report.torn is not None
    assert report.torn.reason == "checksum mismatch in the last record"
    assert engine.state_hash() == reference(FLOW[:-1]).state_hash()


def test_the_log_is_appendable_after_a_torn_tail_is_repaired(tmp_path: Path) -> None:
    live = Market(tmp_path)
    live.run(FLOW[:1_000])
    live.close()
    (segment,) = segment_files(tmp_path)
    with segment.open("ab") as f:
        f.write(b"\x33\x00\x00\x00half a rec")

    resumed = Market(tmp_path)
    resumed.run(FLOW[1_000:])
    resumed.close()
    engine, report = Journal(tmp_path, fsync=False).recover()

    assert report.torn is None
    assert engine.state_hash() == reference(FLOW).state_hash()


def test_verify_mode_reports_a_torn_tail_without_touching_the_file(tmp_path: Path) -> None:
    live = Market(tmp_path)
    live.run(FLOW[:100])
    live.close()
    (segment,) = segment_files(tmp_path)
    with segment.open("ab") as f:
        f.write(b"\x01\x02")
    size = segment.stat().st_size

    _, report = Journal(tmp_path, fsync=False).recover(repair=False)

    assert report.torn is not None
    assert segment.stat().st_size == size


def test_corruption_in_the_middle_of_the_log_stops_recovery(tmp_path: Path) -> None:
    live = Market(tmp_path)
    live.run(FLOW)
    live.close()
    (segment,) = segment_files(tmp_path)
    data = bytearray(segment.read_bytes())
    data[len(data) // 2] ^= 0x10
    segment.write_bytes(bytes(data))

    with pytest.raises(WalCorruptionError):
        Journal(tmp_path, fsync=False).recover()


def test_recovery_replays_only_what_follows_the_snapshot(tmp_path: Path) -> None:
    live = Market(tmp_path)
    live.run(FLOW[:1_500])
    live.checkpoint()
    live.run(FLOW[1_500:])
    live.close()

    engine, report = Journal(tmp_path, fsync=False).recover()

    assert report.snapshot_seq == 1_500
    assert report.replayed == len(FLOW) - 1_500
    assert engine.state_hash() == reference(FLOW).state_hash()


def test_a_bad_snapshot_falls_back_to_the_previous_one(tmp_path: Path) -> None:
    live = Market(tmp_path)
    live.run(FLOW[:500])
    live.checkpoint()
    live.run(FLOW[500:1_500])
    live.checkpoint()
    live.run(FLOW[1_500:])
    live.close()
    newest = sorted((tmp_path / "snapshots").glob("*.snap"))[-1]
    data = bytearray(newest.read_bytes())
    data[-10] ^= 0xFF
    newest.write_bytes(bytes(data))

    engine, report = Journal(tmp_path, fsync=False).recover()

    assert report.rejected_snapshots == [newest.name]
    assert report.snapshot_seq == 500
    assert engine.state_hash() == reference(FLOW).state_hash()


def test_checkpoints_roll_segments_and_prune_what_no_snapshot_needs(tmp_path: Path) -> None:
    live = Market(tmp_path, keep_snapshots=2)
    for start in range(0, 2_000, 500):
        live.run(FLOW[start : start + 500])
        live.checkpoint()
    live.close()

    snapshots = [p.stem.lstrip("0") for p in sorted((tmp_path / "snapshots").glob("*.snap"))]
    segments = [p.stem.lstrip("0") for p in segment_files(tmp_path)]
    assert snapshots == ["1500", "2000"]
    assert segments == ["1501", "2001"]
    engine, report = Journal(tmp_path, fsync=False).recover()
    assert report.snapshot_seq == 2_000
    assert engine.state_hash() == reference(FLOW).state_hash()


def test_a_missing_segment_is_reported_not_skipped(tmp_path: Path) -> None:
    live = Market(tmp_path, keep_snapshots=1)
    live.run(FLOW[:500])
    live.checkpoint()
    live.run(FLOW[500:1_000])
    live.checkpoint()
    live.run(FLOW[1_000:])
    live.close()
    for snap in (tmp_path / "snapshots").glob("*.snap"):
        snap.unlink()

    with pytest.raises(WalCorruptionError, match="missing"):
        Journal(tmp_path, fsync=False).recover()
