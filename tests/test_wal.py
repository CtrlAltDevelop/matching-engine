"""WAL framing, group commit and torn-write detection."""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import cancel, limit, market

from matching_engine.domain import SelfTradePrevention, SequencedCommand, Side, TimeInForce
from matching_engine.wal import (
    MAGIC,
    SegmentReader,
    SegmentWriter,
    WalCorruptionError,
    decode,
    encode,
)

COMMANDS = [
    SequencedCommand(1, 10, limit(1, Side.BUY, 100, 5, account=7)),
    SequencedCommand(
        2,
        20,
        limit(
            2,
            Side.SELL,
            (1 << 63) - 1,
            3,
            tif=TimeInForce.GTC,
            post_only=True,
            stp=SelfTradePrevention.CANCEL_BOTH,
        ),
    ),
    SequencedCommand(3, 30, market(3, Side.SELL, 9, tif=TimeInForce.FOK)),
    SequencedCommand(4, 40, cancel(1, account=7)),
]


def write(path: Path, commands: list[SequencedCommand]) -> list[int]:
    """Write commands one sync at a time; return each record's end offset."""
    writer = SegmentWriter(path, fsync=False)
    ends = []
    for sc in commands:
        writer.append(sc)
        writer.sync()
        ends.append(path.stat().st_size)
    writer.close()
    return ends


def scan(path: Path) -> tuple[list[SequencedCommand], SegmentReader]:
    reader = SegmentReader(path)
    return list(reader), reader


@pytest.mark.parametrize("sc", COMMANDS, ids=lambda sc: type(sc.command).__name__)
def test_every_command_shape_survives_encode_and_decode(sc: SequencedCommand) -> None:
    record = encode(sc)

    assert decode(record[8:]) == sc


def test_a_clean_segment_reads_back_in_order(tmp_path: Path) -> None:
    path = tmp_path / "seg.wal"
    write(path, COMMANDS)

    records, reader = scan(path)

    assert records == COMMANDS
    assert reader.torn is None
    assert reader.valid_end == path.stat().st_size


def test_append_only_buffers_until_sync(tmp_path: Path) -> None:
    path = tmp_path / "seg.wal"
    writer = SegmentWriter(path, fsync=False)
    for sc in COMMANDS:
        writer.append(sc)

    assert path.stat().st_size == len(MAGIC)
    assert writer.pending == len(COMMANDS)
    writer.sync()
    assert scan(path)[0] == COMMANDS
    writer.close()


def test_reopening_appends_after_existing_records(tmp_path: Path) -> None:
    path = tmp_path / "seg.wal"
    write(path, COMMANDS[:2])
    write(path, COMMANDS[2:])

    assert scan(path)[0] == COMMANDS


@pytest.mark.parametrize("cut", [1, 5, 8, 20])
def test_a_truncated_last_record_is_a_torn_tail(tmp_path: Path, cut: int) -> None:
    path = tmp_path / "seg.wal"
    ends = write(path, COMMANDS)
    with path.open("r+b") as f:
        f.truncate(ends[-2] + cut)

    records, reader = scan(path)

    assert records == COMMANDS[:-1]
    assert reader.valid_end == ends[-2]
    assert reader.torn is not None
    assert reader.torn.offset == ends[-2]
    assert reader.torn.discarded == cut


def test_a_corrupted_last_record_is_a_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "seg.wal"
    ends = write(path, COMMANDS)
    data = bytearray(path.read_bytes())
    data[-3] ^= 0xFF
    path.write_bytes(bytes(data))

    records, reader = scan(path)

    assert records == COMMANDS[:-1]
    assert reader.torn is not None
    assert reader.torn.reason == "checksum mismatch in the last record"
    assert reader.valid_end == ends[-2]


def test_a_zero_filled_tail_is_a_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "seg.wal"
    write(path, COMMANDS)
    with path.open("ab") as f:
        f.write(b"\0" * 64)

    records, reader = scan(path)

    assert records == COMMANDS
    assert reader.torn is not None
    assert reader.torn.reason == "zero-filled tail"


def test_corruption_before_valid_records_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "seg.wal"
    ends = write(path, COMMANDS)
    data = bytearray(path.read_bytes())
    data[ends[0] + 12] ^= 0x01  # inside the second record's payload
    path.write_bytes(bytes(data))

    with pytest.raises(WalCorruptionError, match="records after it"):
        scan(path)


def test_a_flipped_length_is_caught_by_the_checksum(tmp_path: Path) -> None:
    path = tmp_path / "seg.wal"
    ends = write(path, COMMANDS)
    data = bytearray(path.read_bytes())
    data[ends[0]] ^= 0x02  # second record's length now points elsewhere
    path.write_bytes(bytes(data))

    with pytest.raises(WalCorruptionError):
        scan(path)


def test_a_half_written_segment_header_is_torn_and_empty(tmp_path: Path) -> None:
    path = tmp_path / "seg.wal"
    path.write_bytes(MAGIC[:3])

    records, reader = scan(path)

    assert records == []
    assert reader.torn is not None
    assert reader.valid_end == 0


def test_a_foreign_file_is_not_mistaken_for_a_segment(tmp_path: Path) -> None:
    path = tmp_path / "seg.wal"
    path.write_bytes(b"definitely not a wal")

    with pytest.raises(WalCorruptionError, match="not a WAL segment"):
        scan(path)
