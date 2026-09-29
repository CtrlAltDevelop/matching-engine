"""Recovery time for a large WAL, with and without a snapshot, plus WAL write cost.

    uv run python bench/bench_recovery.py --events 1000000

Recovery is timed end to end through ``Journal.recover`` — reading segments,
checking every CRC and sequence number, decoding, and re-running the engine —
because that is what a restart actually pays. The group-commit table shows
what one fsync per batch costs on this disk.
"""

from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from pathlib import Path

from _common import machine, stamped_flow

from matching_engine.domain import SequencedCommand
from matching_engine.engine import MatchingEngine
from matching_engine.journal import Journal
from matching_engine.snapshot import encode_snapshot
from matching_engine.wal import SegmentWriter


def build_log(root: Path, commands: list[SequencedCommand], snapshot_at: int | None) -> None:
    journal = Journal(root, fsync=False)
    journal.open(1)
    engine = MatchingEngine()
    for sc in commands:
        journal.append(sc)
        engine.process(sc)
        if sc.seq == snapshot_at:
            journal.sync()
            journal.checkpoint(encode_snapshot(engine, sc.ts), sc.seq)
        elif sc.seq % 4096 == 0:
            journal.sync()
    journal.close()


def timed_recovery(root: Path) -> tuple[float, int, int, str]:
    start = time.perf_counter()
    engine, report = Journal(root, fsync=False).recover()
    elapsed = time.perf_counter() - start
    return elapsed, report.snapshot_seq, report.replayed, engine.state_hash()


def group_commit(root: Path, commands: list[SequencedCommand], batch: int, count: int) -> float:
    """Records per second when every ``batch`` records share one fsync."""
    writer = SegmentWriter(root / f"batch-{batch}.wal", fsync=True)
    start = time.perf_counter()
    for i, sc in enumerate(commands[:count], start=1):
        writer.append(sc)
        if i % batch == 0:
            writer.sync()
    writer.close()
    return count / (time.perf_counter() - start)


def spread(runs: list[tuple[float, int, int, str]]) -> str:
    times = sorted(r[0] for r in runs)
    return f"median {statistics.median(times):.2f} s (min {times[0]:.2f}, max {times[-1]:.2f})"


def size_mb(root: Path, pattern: str) -> float:
    return sum(p.stat().st_size for p in root.rglob(pattern)) / 1e6


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=1_000_000)
    parser.add_argument("--tail", type=int, default=100_000, help="records after the snapshot")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    commands = stamped_flow(args.events)
    print(f"machine: {machine()}")
    with tempfile.TemporaryDirectory() as tmp:
        plain, snap = Path(tmp) / "plain", Path(tmp) / "snap"
        build_log(plain, commands, snapshot_at=None)
        build_log(snap, commands, snapshot_at=args.events - args.tail)

        # A laptop's clock and background load wander, so report the spread.
        full = [timed_recovery(plain) for _ in range(args.runs)]
        tail = [timed_recovery(snap) for _ in range(args.runs)]
        assert len({r[3] for r in full + tail}) == 1, "every recovery must land on one state"
        print(f"WAL: {size_mb(plain, '*.wal'):.1f} MB, snapshot: {size_mb(snap, '*.snap'):.1f} MB")
        replayed = full[0][2]
        print(
            f"recovery, full replay of {replayed:,} records: {spread(full)} "
            f"= {replayed / statistics.median(r[0] for r in full):,.0f} records/s"
        )
        print(
            f"recovery, snapshot at seq {tail[0][1]:,} + {tail[0][2]:,} record tail: {spread(tail)}"
        )

        for batch, count in ((1, 2_000), (64, 64_000), (512, 256_000)):
            rate = group_commit(Path(tmp), commands, batch, count)
            print(f"WAL append + fsync every {batch:>3} records: {rate:,.0f} records/s")


if __name__ == "__main__":
    main()
