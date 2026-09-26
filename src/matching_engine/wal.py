"""Write-ahead log segments: length-framed, checksummed, fixed-layout records.

Segment layout::

    MAGIC (8 bytes)
    record*

    record  = length:u32 | crc:u32 | payload[length]
    crc     = CRC32(length bytes || payload)
    payload = kind:u8 | seq:u64 | ts:i64 | body      (little-endian, no padding)

The CRC covers the length as well as the payload, so a flipped bit in the
length field cannot silently re-frame the rest of the file.

Recovery rules (see ``docs/adr/0002-wal-format-and-fsync-policy.md``):

* A record that runs past end-of-file is a *torn write*: the process died in
  the middle of an append. Everything from that record on is discarded.
* A checksum failure on the **last** record (nothing but end-of-file or zero
  fill after it) is also a torn write — the sector reached the disk only
  partly.
* A checksum failure with valid-looking data *after* it is not a crash
  artefact but corruption of acknowledged data. That is never repaired
  silently: :class:`WalCorruptionError` is raised and an operator decides.

Writing is batched: :meth:`SegmentWriter.append` only buffers, and
:meth:`SegmentWriter.sync` issues one ``write`` and one ``fsync`` for the
whole batch (group commit).
"""

from __future__ import annotations

import os
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

import msgspec

from matching_engine.domain import (
    CancelOrder,
    NewOrder,
    OrderType,
    SelfTradePrevention,
    SequencedCommand,
    Side,
    TimeInForce,
)

MAGIC = b"MEWAL01\n"

_LENGTH = struct.Struct("<I")
_FRAME = struct.Struct("<II")
# kind, seq, ts, order_id, account, side, type, tif | stp << 4, flags, price, qty
_NEW = struct.Struct("<BQqQQBBBBqq")
# kind, seq, ts, order_id, account
_CANCEL = struct.Struct("<BQqQQ")
_MAX_PAYLOAD = max(_NEW.size, _CANCEL.size)

_KIND_NEW = 1
_KIND_CANCEL = 2
_FLAG_POST_ONLY = 0x01

# Enum <-> byte code tables. Codes are positions in these tuples, so new
# members may only ever be appended — reordering would reinterpret old logs.
_SIDES = (Side.BUY, Side.SELL)
_TYPES = (OrderType.LIMIT, OrderType.MARKET)
_TIFS = (TimeInForce.GTC, TimeInForce.IOC, TimeInForce.FOK)
_STPS = (
    SelfTradePrevention.NONE,
    SelfTradePrevention.CANCEL_TAKER,
    SelfTradePrevention.CANCEL_MAKER,
    SelfTradePrevention.CANCEL_BOTH,
)
_SIDE_CODE = {m: i for i, m in enumerate(_SIDES)}
_TYPE_CODE = {m: i for i, m in enumerate(_TYPES)}
_TIF_CODE = {m: i for i, m in enumerate(_TIFS)}
_STP_CODE = {m: i for i, m in enumerate(_STPS)}


class WalCorruptionError(Exception):
    """The log is damaged somewhere other than a torn tail. Needs a human."""


class TornTail(msgspec.Struct, frozen=True):
    """An incomplete write at the end of a segment, found during a scan."""

    offset: int  # where the damaged record starts; the valid log ends here
    discarded: int  # bytes from ``offset`` to end-of-file
    reason: str


def encode(sc: SequencedCommand) -> bytes:
    """Frame one command as a complete record, header included."""
    cmd = sc.command
    if type(cmd) is NewOrder:
        payload = _NEW.pack(
            _KIND_NEW,
            sc.seq,
            sc.ts,
            cmd.order_id,
            cmd.account,
            _SIDE_CODE[cmd.side],
            _TYPE_CODE[cmd.order_type],
            _TIF_CODE[cmd.tif] | (_STP_CODE[cmd.stp] << 4),
            _FLAG_POST_ONLY if cmd.post_only else 0,
            cmd.price,
            cmd.qty,
        )
    else:
        assert type(cmd) is CancelOrder
        payload = _CANCEL.pack(_KIND_CANCEL, sc.seq, sc.ts, cmd.order_id, cmd.account)
    length = len(payload)
    return _FRAME.pack(length, zlib.crc32(payload, zlib.crc32(_LENGTH.pack(length)))) + payload


def decode(payload: bytes) -> SequencedCommand:
    kind = payload[0]
    if kind == _KIND_NEW and len(payload) == _NEW.size:
        _, seq, ts, oid, account, side, otype, tif_stp, flags, price, qty = _NEW.unpack(payload)
        command: NewOrder | CancelOrder = NewOrder(
            oid,
            account,
            _SIDES[side],
            price,
            qty,
            _TYPES[otype],
            _TIFS[tif_stp & 0x0F],
            bool(flags & _FLAG_POST_ONLY),
            _STPS[tif_stp >> 4],
        )
    elif kind == _KIND_CANCEL and len(payload) == _CANCEL.size:
        _, seq, ts, oid, account = _CANCEL.unpack(payload)
        command = CancelOrder(oid, account)
    else:
        raise WalCorruptionError(f"unknown record kind {kind} with {len(payload)} bytes")
    return SequencedCommand(seq, ts, command)


def _is_blank(data: bytes, start: int) -> bool:
    """True if nothing but zero bytes (or nothing at all) follows ``start``.

    Some filesystems extend a file with zeros before the data lands, so a
    crash can leave a zero-filled tail rather than a short one.
    """
    return not data[start:].strip(b"\0")


class SegmentReader:
    """Iterate the valid records of one segment, then report how it ended.

    After iteration, ``valid_end`` is the byte offset just past the last good
    record and ``torn`` describes any incomplete tail (``None`` if clean).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.valid_end = 0
        self.torn: TornTail | None = None

    def __iter__(self) -> Iterator[SequencedCommand]:
        data = self.path.read_bytes()
        size = len(data)
        header = len(MAGIC)
        if size < header:
            # The process died while creating the segment.
            if size:
                self.torn = TornTail(0, size, "incomplete segment header")
            return
        if data[:header] != MAGIC:
            raise WalCorruptionError(f"{self.path}: not a WAL segment")

        frame_size = _FRAME.size
        unpack_frame = _FRAME.unpack_from
        crc32 = zlib.crc32
        pos = self.valid_end = header
        while pos < size:
            if size - pos < frame_size:
                self._tear(pos, size, "incomplete record header")
                return
            length, crc = unpack_frame(data, pos)
            start = pos + frame_size
            end = start + length
            if not 0 < length <= _MAX_PAYLOAD:
                if _is_blank(data, pos):
                    self._tear(pos, size, "zero-filled tail")
                    return
                raise WalCorruptionError(f"{self.path}: bad record length {length} at {pos}")
            if end > size:
                self._tear(pos, size, "incomplete record")
                return
            payload = data[start:end]
            if crc32(payload, crc32(data[pos : pos + 4])) != crc:
                if _is_blank(data, end):
                    self._tear(pos, size, "checksum mismatch in the last record")
                    return
                raise WalCorruptionError(
                    f"{self.path}: checksum mismatch at offset {pos} with records after it"
                )
            yield decode(payload)
            pos = self.valid_end = end

    def _tear(self, offset: int, size: int, reason: str) -> None:
        self.torn = TornTail(offset, size - offset, reason)


class SegmentWriter:
    """Append-only writer for one segment with explicit group commit."""

    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        self.path = path
        self._fsync = fsync
        self._buffer = bytearray()
        self.pending = 0  # records buffered since the last sync
        self._file: BinaryIO = path.open("ab")
        if self._file.tell() == 0:
            self._file.write(MAGIC)
            self._flush()
            fsync_directory(path.parent)

    def append(self, sc: SequencedCommand) -> None:
        self._buffer += encode(sc)
        self.pending += 1

    def sync(self) -> None:
        """Make every appended record durable: one write, one fsync."""
        if not self._buffer:
            return
        self._file.write(self._buffer)
        self._buffer.clear()
        self.pending = 0
        self._flush()

    def close(self) -> None:
        self.sync()
        self._file.close()

    def _flush(self) -> None:
        self._file.flush()
        if self._fsync:
            os.fsync(self._file.fileno())


def fsync_directory(path: Path) -> None:
    """Persist a directory entry (a new or renamed file) on POSIX.

    Windows has no way to open a directory for fsync; NTFS journals metadata
    itself, so there the call is a no-op.
    """
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
