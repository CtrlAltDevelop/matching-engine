import json
from pathlib import Path

import pytest

from matching_engine.cli import main
from matching_engine.domain import NewOrder, SequencedCommand, Side
from matching_engine.journal import Journal


def test_verify_prints_the_state_hash_of_a_healthy_market(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = Journal(tmp_path, fsync=False)
    journal.open(next_seq=1)
    journal.append(SequencedCommand(1, 1, NewOrder(1, 1, Side.BUY, 100, 5)))
    journal.close()

    code = main(["verify", str(tmp_path)])

    out = json.loads(capsys.readouterr().out)
    engine, _ = Journal(tmp_path, fsync=False).recover()
    assert code == 0
    assert out["state_hash"] == engine.state_hash()
    assert out["best_bid"] == 100


def test_verify_fails_on_a_corrupted_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "wal").mkdir()
    (tmp_path / "wal" / f"{1:020d}.wal").write_bytes(b"garbage!garbage!")

    assert main(["verify", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
