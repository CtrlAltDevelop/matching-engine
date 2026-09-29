"""Engine throughput and per-command latency, in-process, no I/O.

    uv run python bench/bench_engine.py --orders 1000000

Throughput is measured on one pass with no per-command timing. Latency is a
second pass on a fresh engine that wraps every ``process`` call in
``perf_counter_ns``; the timer's own cost (tens of nanoseconds) is included.
"""

from __future__ import annotations

import argparse
import gc
import time

from _common import machine, percentile, stamped_flow

from matching_engine.domain import SequencedCommand
from matching_engine.engine import MatchingEngine
from matching_engine.events import Trade


def throughput(commands: list[SequencedCommand]) -> tuple[float, int, MatchingEngine]:
    engine = MatchingEngine()
    process = engine.process
    trades = 0
    start = time.perf_counter()
    for sc in commands:
        for e in process(sc):
            if type(e) is Trade:
                trades += 1
    elapsed = time.perf_counter() - start
    return len(commands) / elapsed, trades, engine


def latencies(commands: list[SequencedCommand]) -> list[int]:
    engine = MatchingEngine()
    process = engine.process
    clock = time.perf_counter_ns
    samples = [0] * len(commands)
    for i, sc in enumerate(commands):
        t0 = clock()
        process(sc)
        samples[i] = clock() - t0
    samples.sort()
    return samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", type=int, default=1_000_000)
    parser.add_argument("--runs", type=int, default=3, help="throughput runs; best is reported")
    args = parser.parse_args()

    commands = stamped_flow(args.orders)
    gc.disable()  # measure the engine, not the collector's pauses on a growing heap
    rates = []
    for _ in range(args.runs):
        rate, trades, engine = throughput(commands)
        rates.append(rate)
    samples = latencies(commands)
    gc.enable()

    print(f"machine: {machine()}")
    print(f"commands: {len(commands):,}  trades: {trades:,}  resting at end: {len(engine.book):,}")
    print(f"throughput: best {max(rates):,.0f} cmd/s, runs {[f'{r:,.0f}' for r in rates]}")
    print(
        "latency (us): "
        f"p50 {percentile(samples, 50):.2f}  p99 {percentile(samples, 99):.2f}  "
        f"p99.9 {percentile(samples, 99.9):.2f}  max {samples[-1] / 1_000:.1f}"
    )


if __name__ == "__main__":
    main()
