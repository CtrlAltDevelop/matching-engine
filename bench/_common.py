"""Shared helpers for the benchmark scripts: workload, timing and reporting."""

from __future__ import annotations

import itertools
import os
import platform
import sys

from matching_engine.domain import SequencedCommand
from matching_engine.sequencer import Sequencer
from matching_engine.workload import order_flow

SEED = 42


def stamped_flow(count: int, seed: int = SEED) -> list[SequencedCommand]:
    """Pre-stamp the workload so generation cost stays out of the measurement."""
    sequencer = Sequencer(clock=itertools.count(1).__next__)
    return [sequencer.stamp(c) for c in order_flow(seed, count)]


def percentile(sorted_ns: list[int], pct: float) -> float:
    """Nearest-rank percentile of pre-sorted nanosecond samples, in microseconds."""
    rank = max(0, min(len(sorted_ns) - 1, round(pct / 100 * len(sorted_ns)) - 1))
    return sorted_ns[rank] / 1_000


def cpu_name() -> str:
    """The marketing CPU name; ``platform.processor()`` only gives a family code on Windows."""
    try:
        import winreg  # noqa: PLC0415 - Windows only
    except ImportError:
        return platform.processor() or platform.machine()
    path = chr(92).join(("HARDWARE", "DESCRIPTION", "System", "CentralProcessor", "0"))
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:  # type: ignore[attr-defined]
        return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()  # type: ignore[attr-defined]


def machine() -> str:
    return (
        f"{platform.system()} {platform.release()} ({platform.version()}), "
        f"{cpu_name()}, {os.cpu_count()} logical CPUs, "
        f"Python {sys.version.split()[0]} ({platform.python_implementation()})"
    )
