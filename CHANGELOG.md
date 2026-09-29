# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). For this project
the WAL and snapshot formats are part of the public interface: a release that
cannot read the previous release's files is a major version.

## [Unreleased]

## [0.1.0] — 2026-09-29

### Added

- Price-time priority order book: sorted price levels of intrusive FIFO
  queues, O(1) cancel through an order-id index, integer ticks and lots.
- Limit and market orders; GTC, IOC and FOK; post-only; self-trade
  prevention (cancel taker, cancel maker, cancel both, or allow); owner-only
  cancels.
- An ordered event stream per command: `OrderAccepted`, `Trade`,
  `OrderCancelled`, `OrderRejected`, each carrying the command's `seq`/`ts`.
- A sequencer that is the only source of time and ordering, so the engine is
  deterministic and `state_hash()` identifies a book exactly.
- Write-ahead log: length-framed records with a CRC32 over length and
  payload, group commit with one fsync per batch, torn-tail detection and
  repair, refusal to "repair" corruption that is followed by valid records.
- Snapshots written atomically and verified by state hash on load; recovery
  from the newest valid snapshot with fallback to older ones; segment rolling
  and retention.
- FastAPI gateway: REST order entry and cancel, L2 depth, L3 book, health;
  one sequenced worker per market; 503 instead of unbounded queueing.
- WebSocket market data: depth snapshot on connect, then trades and depth
  changes; slow subscribers are dropped instead of slowing the market.
- `matching-engine verify` to inspect a market directory without changing it.
- Golden, property-based, replay, crash (killed subprocess) and gateway tests;
  engine, recovery and end-to-end benchmarks.

[Unreleased]: https://github.com/CtrlAltDevelop/matching-engine/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/CtrlAltDevelop/matching-engine/releases/tag/v0.1.0
