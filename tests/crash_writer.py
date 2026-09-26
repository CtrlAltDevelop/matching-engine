"""Child process for the crash test: log a seeded order flow until it is killed.

After every group commit it prints the last durable sequence number, which is
exactly what a gateway would have acknowledged to clients at that point.

    python tests/crash_writer.py <data-dir> <seed>
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

from matching_engine.journal import Journal
from matching_engine.sequencer import Sequencer
from matching_engine.workload import order_flow

BATCH = 64


def main(root: Path, seed: int) -> None:
    journal = Journal(root, fsync=True)
    engine, report = journal.recover()
    sequencer = Sequencer(report.last_seq + 1, report.last_ts, itertools.count(1).__next__)
    journal.open(sequencer.next_seq)
    flow = order_flow(seed, count=10_000_000)
    while batch := list(itertools.islice(flow, BATCH)):
        stamped = [sequencer.stamp(c) for c in batch]
        for sc in stamped:
            journal.append(sc)
        journal.sync()
        for sc in stamped:
            engine.process(sc)
        print(stamped[-1].seq, flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]))
