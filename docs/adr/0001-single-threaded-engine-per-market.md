# 1. One single-threaded engine per market

- Status: accepted
- Date: 2026-09-26

## Context

A matching engine has to answer two questions about every order: *in what
order did things happen*, and *what did the book look like when this order
arrived*. Both answers must be the same today, after a crash, and when an
auditor replays the day next year.

Inside one market, every order can touch the same few price levels. Running
two orders in parallel means a lock around the book (so they run one after
the other anyway, plus the cost of the lock) or a lock-free structure whose
behaviour under contention is hard to reason about and harder to replay.
Worse, the *outcome* would then depend on thread scheduling: which of two
simultaneous aggressive orders gets the last lot is decided by the OS, not
by the log.

Across markets, nothing is shared. `BTC-USDT` and `ETH-USDT` have separate
books, separate sequence numbers and separate logs.

## Decision

- Each market is owned by exactly one worker (`gateway/worker.py`). Only that
  worker calls `MatchingEngine.process`; nothing else mutates the book.
- The engine itself contains no clock, no randomness, no I/O and no locks.
  Time and order come from the sequencer, which stamps each command before it
  is logged (`sequencer.py`).
- Concurrency is between markets, never within one. Each market has its own
  directory, WAL, snapshots and sequence.
- Order ids are the market's sequence number, so they are unique and
  reproducible without a second counter to persist.

## Consequences

- **Determinism is structural, not tested-in.** The same WAL always produces
  the same events and the same `state_hash()`; the replay and crash tests
  check it, but no code path could break it short of adding a clock.
- **Throughput per market is bounded by one core.** That is the same trade
  LMAX made: a single business-logic thread fed by a ring buffer, with
  journalling and replication around it. The measured single-core number is
  in the README; an engine that is fast on one core and trivially correct is
  worth more than one that is faster on eight and occasionally wrong.
- **Hot markets cannot borrow capacity from quiet ones.** Scaling is by
  splitting markets across processes or hosts, which needs no coordination.
- Today all market workers are asyncio tasks in one process, so they share
  the GIL. They share no *state*, which is what the design needs; moving each
  worker into its own process (or a free-threaded interpreter) changes
  deployment, not the engine. See the roadmap.
- Read-only views (depth, L3) are served from the same event loop between
  commands, so they never see a half-applied command and need no locking.
