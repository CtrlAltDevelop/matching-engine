"""The sequencer: the one place where time and ordering enter the system.

Everything downstream — the WAL, the engine, market data — sees commands in
the order and with the timestamps the sequencer gave them. Keeping the clock
here, outside the engine, is what makes replay deterministic.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from matching_engine.domain import Command, SequencedCommand


class Sequencer:
    """Assigns gap-free sequence numbers and non-decreasing timestamps.

    The wall clock can step backwards (NTP slew, a VM migration); timestamps
    are clamped so the log never shows time running in reverse.
    """

    __slots__ = ("_clock", "last_ts", "next_seq")

    def __init__(
        self,
        next_seq: int = 1,
        last_ts: int = 0,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self.next_seq = next_seq
        self.last_ts = last_ts
        self._clock = clock

    def stamp(self, command: Command) -> SequencedCommand:
        ts = self._clock()
        ts = max(ts, self.last_ts)
        self.last_ts = ts
        seq = self.next_seq
        self.next_seq = seq + 1
        return SequencedCommand(seq, ts, command)
