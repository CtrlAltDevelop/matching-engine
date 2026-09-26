"""Kill a real writer process mid-load and check that recovery keeps every ack."""

from __future__ import annotations

import itertools
import subprocess
import sys
from pathlib import Path

import pytest

from matching_engine.engine import MatchingEngine
from matching_engine.journal import Journal
from matching_engine.sequencer import Sequencer
from matching_engine.workload import order_flow

WRITER = Path(__file__).with_name("crash_writer.py")
SEED = 1234


def replay_in_memory(count: int) -> MatchingEngine:
    engine = MatchingEngine()
    sequencer = Sequencer(clock=itertools.count(1).__next__)
    for command in order_flow(SEED, count=count):
        engine.process(sequencer.stamp(command))
    return engine


@pytest.mark.slow
@pytest.mark.parametrize("kill_after", [1_000, 5_000])
def test_a_killed_writer_loses_nothing_it_acknowledged(tmp_path: Path, kill_after: int) -> None:
    child = subprocess.Popen(
        [sys.executable, str(WRITER), str(tmp_path), str(SEED)],
        stdout=subprocess.PIPE,
        text=True,
    )
    acked = 0
    try:
        assert child.stdout is not None
        for line in child.stdout:
            acked = int(line)
            if acked >= kill_after:
                break
    finally:
        child.kill()  # SIGKILL on POSIX, TerminateProcess on Windows: no cleanup runs
        child.wait()
    assert acked >= kill_after, "the writer died before reaching the kill point"

    engine, report = Journal(tmp_path).recover()

    # Records written after the last ack may or may not have survived; every
    # acknowledged one must have, and whatever did survive must replay exactly.
    assert report.last_seq >= acked
    assert engine.state_hash() == replay_in_memory(report.last_seq).state_hash()
