# 4. Python first, with a clean path to a Rust core

- Status: accepted
- Date: 2026-09-26

## Context

The obvious language for a matching engine is one without a garbage
collector or an interpreter: Rust, C++, or Java tuned the way LMAX tunes it.
The development machine for this project has no Rust toolchain, and a
correct, tested engine matters more than a fast one that nobody can check.

The usual failure of "we'll port it later" is that the prototype's structure
never allowed it: the engine reaches into the database, reads the clock,
takes callbacks from the web framework, and the port becomes a rewrite.

## Decision

Write the engine in plain, typed Python (3.12+, `mypy --strict`), but draw
its boundary exactly where a native core would sit:

- **Input:** `SequencedCommand` — a plain struct of integers and small enums.
- **Output:** a list of event structs — again integers and small enums.
- **State export:** `export_state()` / `restore()` and `state_hash()`.
- **No dependencies on the outside world:** no clock, no I/O, no framework
  types, no callbacks. `engine.py` and `book.py` import only the standard
  library, `msgspec` and `sortedcontainers`.

Everything else — sequencing, the WAL, snapshots, the gateway, market data —
stays in Python and talks to the engine only through that boundary.

## Consequences

- A PyO3 module exposing `process(seq, ts, command) -> events` plus the three
  state functions is a drop-in replacement. The golden files, property tests
  and replay tests then serve as its acceptance suite unchanged: the new core
  is correct when it produces byte-identical events and the same state hash
  from the same WAL.
- The book structures map directly: `BTreeMap<i64, Level>` per side for the
  sorted keys, a slab of nodes with index-based links for the FIFO queues,
  and a `HashMap<u64, NodeIndex>` for O(1) cancel.
- The WAL format is fixed-width little-endian and documented in `wal.py`, so
  a native reader needs no Python at all.
- Until then, the measured numbers in the README are the pure-Python ones,
  and the gateway rather than the engine is the throughput bottleneck. No
  Rust number is published here because none has been measured.
