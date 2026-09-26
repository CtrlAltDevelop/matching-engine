"""Point-in-time snapshots of an engine, so recovery replays a tail, not a history.

File layout::

    MAGIC (8 bytes) | crc:u32 | length:u64 | body[length]
    body = MessagePack(Snapshot)

A snapshot is written to a temporary name, fsynced, then atomically renamed,
so a crash leaves either the old snapshot set or the new one — never half a
file under a final name. The stored state hash is re-checked after loading:
a snapshot that decodes but rebuilds a different book is rejected, too.
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

import msgspec

from matching_engine.engine import EngineState, MatchingEngine
from matching_engine.wal import fsync_directory

MAGIC = b"MESNAP1\n"
_HEADER = struct.Struct("<IQ")


class SnapshotError(Exception):
    """A snapshot file is unreadable or does not reproduce its recorded state."""


class Snapshot(msgspec.Struct, frozen=True):
    state: EngineState
    state_hash: str
    last_ts: int  # the sequencer resumes from here so timestamps stay monotonic


def encode_snapshot(engine: MatchingEngine, last_ts: int) -> bytes:
    """Serialize the engine. Must run while nothing is mutating it."""
    body = msgspec.msgpack.encode(Snapshot(engine.export_state(), engine.state_hash(), last_ts))
    return MAGIC + _HEADER.pack(zlib.crc32(body), len(body)) + body


def write_snapshot(path: Path, blob: bytes, *, fsync: bool = True) -> None:
    """Durably place an encoded snapshot at ``path``. Safe to run off-thread."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        f.write(blob)
        f.flush()
        if fsync:
            os.fsync(f.fileno())
    tmp.replace(path)
    if fsync:
        fsync_directory(path.parent)


def read_snapshot(path: Path) -> tuple[MatchingEngine, Snapshot]:
    data = path.read_bytes()
    head = len(MAGIC) + _HEADER.size
    if len(data) < head or data[: len(MAGIC)] != MAGIC:
        raise SnapshotError(f"{path}: not a snapshot")
    crc, length = _HEADER.unpack_from(data, len(MAGIC))
    body = data[head:]
    if len(body) != length or zlib.crc32(body) != crc:
        raise SnapshotError(f"{path}: checksum or length mismatch")
    try:
        snap = msgspec.msgpack.decode(body, type=Snapshot)
    except msgspec.DecodeError as exc:
        raise SnapshotError(f"{path}: {exc}") from exc
    engine = MatchingEngine.restore(snap.state)
    if engine.state_hash() != snap.state_hash:
        raise SnapshotError(f"{path}: rebuilt state does not match the recorded hash")
    return engine, snap
