"""Public market data fan-out: trades and L2 depth for WebSocket subscribers.

The matching loop must never wait for a reader. Each subscriber gets a
bounded queue; publishing is ``put_nowait``, and a subscriber whose queue is
full is disconnected rather than allowed to slow the market down or grow
memory without bound. A client that reconnects gets a fresh depth snapshot,
which is always a correct place to resume from.

Depth is pushed as a full top-``n`` snapshot after any batch that changed it.
That costs more bandwidth than incremental deltas, but a client can never
apply one out of order or miss one and drift.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import msgspec

from matching_engine.book import Depth, DepthLevel
from matching_engine.engine import MatchingEngine
from matching_engine.events import Event, Trade
from matching_engine.market import MarketSpec


class Subscriber:
    __slots__ = ("queue",)

    def __init__(self, maxsize: int) -> None:
        # ``None`` is the hang-up sentinel: the hub dropped this subscriber.
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize + 1)


class MarketDataHub:
    def __init__(self, spec: MarketSpec, *, depth_levels: int, subscriber_queue: int) -> None:
        self.spec = spec
        self._depth_levels = depth_levels
        self._subscriber_queue = subscriber_queue
        self._subscribers: set[Subscriber] = set()
        self._last_depth: Depth | None = None
        self.dropped = 0  # subscribers disconnected for falling behind

    def subscribe(self, engine: MatchingEngine) -> tuple[Subscriber, str]:
        """Register a subscriber and return the depth snapshot to send it first."""
        subscriber = Subscriber(self._subscriber_queue)
        self._subscribers.add(subscriber)
        depth = engine.book.depth(self._depth_levels)
        self._last_depth = depth
        return subscriber, self._depth_message(engine.last_seq, depth)

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    def publish(self, events: Iterable[Event], engine: MatchingEngine) -> None:
        """Push one batch's trades, then the new depth if it changed."""
        if not self._subscribers:
            return
        spec = self.spec
        for e in events:
            if type(e) is Trade:
                trade = {
                    "type": "trade",
                    "symbol": spec.symbol,
                    "seq": e.seq,
                    "ts": e.ts,
                    "trade_id": e.trade_id,
                    "price": spec.price(e.price),
                    "qty": spec.qty(e.qty),
                    "taker_side": e.taker_side,
                }
                self._broadcast(msgspec.json.encode(trade).decode())
        depth = engine.book.depth(self._depth_levels)
        if depth != self._last_depth:
            self._last_depth = depth
            self._broadcast(self._depth_message(engine.last_seq, depth))

    def _depth_message(self, seq: int, depth: Depth) -> str:
        spec = self.spec

        def side(levels: list[DepthLevel]) -> list[list[object]]:
            return [[spec.price(lvl.price), spec.qty(lvl.qty), lvl.orders] for lvl in levels]

        message = {
            "type": "depth",
            "symbol": spec.symbol,
            "seq": seq,
            "bids": side(depth.bids),
            "asks": side(depth.asks),
        }
        return msgspec.json.encode(message).decode()

    def _broadcast(self, message: str) -> None:
        for subscriber in list(self._subscribers):
            queue = subscriber.queue
            if queue.qsize() >= self._subscriber_queue:
                self._drop(subscriber)
            else:
                queue.put_nowait(message)

    def _drop(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)
        self.dropped += 1
        subscriber.queue.put_nowait(None)  # the spare slot reserved for exactly this
