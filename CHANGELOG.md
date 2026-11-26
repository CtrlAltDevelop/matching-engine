# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). For this project
the WAL and snapshot formats are part of the public interface: a release that
cannot read the previous release's files is a major version.

## [Unreleased]

## [0.2.0] — 2026-11-26

### Added

- Amend: `PATCH /markets/{symbol}/orders/{order_id}?account=` with a new price
  and total remaining quantity. Lowering size at the same price keeps queue
  position; any other change re-queues at the back and matches if it crosses.
  New `ReplaceOrder` command and `order_replaced` event.
- Stop and stop-limit orders: set `stop_price` on a new order. It is parked
  off the book until the last trade price reaches it (buy: at or above, sell:
  at or below), then submitted in arrival order, cascading within the same
  command. New `order_triggered` event and `pending` order status; parked
  stops can be cancelled. See ADR 5.
- ADR 5, recording the stop trigger and amend decisions.

### Changed

- WAL: replace is record kind 3 and a stop order is kind 4. Existing logs
  replay unchanged. Snapshots gain `last_trade_price` and `stops`, both
  defaulted, so existing snapshots load. The engine state hash now covers
  them, so hashes differ from 0.1.x.
- Dependencies: the python-minor group (14 updates); Docker base image
  python 3.15-slim.

## [0.1.1] — 2026-10-15

### Fixed

- Prices and quantities with more digits than the decimal context holds were
  silently rounded onto the grid; they are now refused. A huge exponent
  (`1e999999`) returned a 500 instead of a 422.
- A WebSocket feed cancelled by server shutdown left its pump and hang-up
  tasks running, and a send error on a dead socket was logged as an
  unretrieved task exception.

### Changed

- Dependencies: fastapi 0.143.1, starlette 1.8.0, pydantic 2.14.1,
  httptools 0.9.0, ruff 0.17.0.

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

[Unreleased]: https://github.com/CtrlAltDevelop/matching-engine/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/CtrlAltDevelop/matching-engine/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/CtrlAltDevelop/matching-engine/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/CtrlAltDevelop/matching-engine/releases/tag/v0.1.0
