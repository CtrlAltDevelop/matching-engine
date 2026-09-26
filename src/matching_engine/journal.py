"""A market's durable history: WAL segments plus the snapshots that bound replay.

Directory layout::

    <root>/wal/00000000000000000001.wal        first seq in each segment
    <root>/wal/00000000000000500001.wal
    <root>/snapshots/00000000000000500000.snap  state after that seq

Every checkpoint writes a snapshot at ``seq`` and starts a new segment at
``seq + 1``, so segment boundaries line up with snapshots and old history can
be dropped a whole file at a time.

Recovery loads the newest snapshot that verifies (falling back to older ones
if it does not), then replays every later record in order, checking that
sequence numbers are gap-free across segments. A torn tail is legal only in
the newest segment; it is truncated away so the writer appends onto a clean
end.
"""

from __future__ import annotations

import itertools
import logging
from pathlib import Path

import msgspec

from matching_engine.domain import SequencedCommand
from matching_engine.engine import MatchingEngine
from matching_engine.snapshot import SnapshotError, read_snapshot, write_snapshot
from matching_engine.wal import SegmentReader, SegmentWriter, TornTail, WalCorruptionError

log = logging.getLogger(__name__)

_SEGMENT_SUFFIX = ".wal"
_SNAPSHOT_SUFFIX = ".snap"


class RecoveryReport(msgspec.Struct, frozen=True):
    snapshot_seq: int  # 0 when recovery started from an empty engine
    replayed: int  # records applied on top of the snapshot
    last_seq: int
    last_ts: int
    torn: TornTail | None  # the incomplete write that was (or would be) cut off
    rejected_snapshots: list[str]  # snapshots skipped because they failed to verify


class Journal:
    def __init__(self, root: Path, *, fsync: bool = True, keep_snapshots: int = 2) -> None:
        if keep_snapshots < 1:
            raise ValueError("keep at least one snapshot")
        self.root = root
        self.fsync = fsync
        self.keep_snapshots = keep_snapshots
        self.wal_dir = root / "wal"
        self.snapshot_dir = root / "snapshots"
        self.wal_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._writer: SegmentWriter | None = None
        self._segment_first_seq = 0

    # -- recovery -----------------------------------------------------------

    def recover(self, *, repair: bool = True) -> tuple[MatchingEngine, RecoveryReport]:
        """Rebuild the engine from disk.

        With ``repair`` (the default) a torn tail is truncated so the log can
        be appended to; without it the files are left untouched, which is
        what an offline ``verify`` wants.
        """
        engine, last_ts, rejected = self._load_snapshot()
        snapshot_seq = engine.last_seq
        segments = self._segments()
        replayed = 0
        torn: TornTail | None = None
        for i, (first_seq, path) in enumerate(segments):
            is_last = i == len(segments) - 1
            if not is_last and segments[i + 1][0] - 1 <= engine.last_seq:
                continue  # every record here is already inside the snapshot
            reader = SegmentReader(path)
            expected = first_seq
            for sc in reader:
                if sc.seq != expected:
                    raise WalCorruptionError(f"{path}: expected seq {expected}, found {sc.seq}")
                expected += 1
                if sc.seq <= engine.last_seq:
                    continue
                if sc.seq != engine.last_seq + 1:
                    raise WalCorruptionError(
                        f"{path}: history is missing between seq {engine.last_seq} and {sc.seq}"
                    )
                engine.process(sc)
                last_ts = sc.ts
                replayed += 1
            if reader.torn is not None:
                if not is_last:
                    raise WalCorruptionError(f"{path}: torn write inside a sealed segment")
                torn = reader.torn
                log.warning("torn WAL tail in %s: %s", path.name, torn)
                if repair:
                    with path.open("r+b") as f:
                        f.truncate(reader.valid_end)
        report = RecoveryReport(snapshot_seq, replayed, engine.last_seq, last_ts, torn, rejected)
        return engine, report

    def _load_snapshot(self) -> tuple[MatchingEngine, int, list[str]]:
        rejected: list[str] = []
        for _, path in reversed(self._snapshots()):
            try:
                engine, snap = read_snapshot(path)
            except SnapshotError as exc:
                log.error("skipping snapshot: %s", exc)
                rejected.append(path.name)
                continue
            return engine, snap.last_ts, rejected
        return MatchingEngine(), 0, rejected

    # -- writing ------------------------------------------------------------

    def open(self, next_seq: int) -> None:
        """Start appending after recovery: onto the newest segment, or a new one."""
        segments = self._segments()
        if segments:
            self._segment_first_seq, path = segments[-1]
        else:
            self._segment_first_seq, path = next_seq, self._segment_path(next_seq)
        self._writer = SegmentWriter(path, fsync=self.fsync)

    def append(self, sc: SequencedCommand) -> None:
        assert self._writer is not None, "open() the journal before appending"
        self._writer.append(sc)

    def sync(self) -> None:
        """Group commit: make every appended record durable at once."""
        assert self._writer is not None
        self._writer.sync()

    def checkpoint(self, blob: bytes, seq: int) -> None:
        """Persist an encoded snapshot taken at ``seq`` and roll to a new segment.

        The caller encodes the snapshot (see ``snapshot.encode_snapshot``)
        while the engine is quiescent; this part only does I/O, so it can run
        on a worker thread.
        """
        assert self._writer is not None
        write_snapshot(self._snapshot_path(seq), blob, fsync=self.fsync)
        if self._segment_first_seq != seq + 1:
            self._writer.close()
            self._segment_first_seq = seq + 1
            self._writer = SegmentWriter(self._segment_path(seq + 1), fsync=self.fsync)
        self._prune()

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def _prune(self) -> None:
        """Drop snapshots beyond the retention count and segments none of them need."""
        snapshots = self._snapshots()
        for _, path in snapshots[: -self.keep_snapshots]:
            path.unlink()
        oldest_kept = snapshots[-self.keep_snapshots :][0][0]
        segments = self._segments()
        for (_, path), (next_first, _) in itertools.pairwise(segments):
            if next_first - 1 <= oldest_kept:
                path.unlink()

    # -- layout -------------------------------------------------------------

    def _segment_path(self, first_seq: int) -> Path:
        return self.wal_dir / f"{first_seq:020d}{_SEGMENT_SUFFIX}"

    def _snapshot_path(self, seq: int) -> Path:
        return self.snapshot_dir / f"{seq:020d}{_SNAPSHOT_SUFFIX}"

    def _segments(self) -> list[tuple[int, Path]]:
        return _numbered(self.wal_dir, _SEGMENT_SUFFIX)

    def _snapshots(self) -> list[tuple[int, Path]]:
        return _numbered(self.snapshot_dir, _SNAPSHOT_SUFFIX)


def _numbered(directory: Path, suffix: str) -> list[tuple[int, Path]]:
    """Files named ``<number><suffix>``, sorted by number."""
    found = [(int(p.stem), p) for p in directory.glob(f"*{suffix}") if p.stem.isdigit()]
    return sorted(found)
