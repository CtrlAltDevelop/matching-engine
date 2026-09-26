"""The order book: two sides of price levels, each level a FIFO queue.

Layout
------
Each side keeps

* ``levels``: ``dict[key, PriceLevel]`` — O(1) lookup of an existing level;
* ``keys``: a ``SortedList`` of the same keys — ordered, best first.

The *key* is the price for asks and the negated price for bids, so on both
sides ``keys[0]`` is the best level and "does this level cross a limit" is the
single comparison ``key <= limit_key``. The matcher never branches on side
inside its loop.

A level is an intrusive doubly linked list of :class:`RestingOrder` nodes, and
the book holds ``dict[order_id, RestingOrder]``. Together they make cancel O(1):
find the node by id, unlink it, no scan of the queue. A ``deque`` would give
O(1) append and pop-left, but removal from the middle is O(n).

=====================================  ==========
Operation                              Complexity
=====================================  ==========
Add to an existing level               O(1)
Add at a new price level               O(log L)
Cancel (level stays non-empty)         O(1)
Cancel (last order at its level)       O(log L)
Best bid / ask                         O(1)
Fill against the head of best level    O(1)
L2 depth, top ``n`` levels             O(n)
=====================================  ==========

``L`` is the number of distinct price levels on one side, which in practice is
far smaller than the number of orders.
"""

from __future__ import annotations

from collections.abc import Iterator

import msgspec
from sortedcontainers import SortedList

from matching_engine.domain import Side


class RestingOrder:
    """A live order on the book. Mutable: fills decrement ``remaining`` in place."""

    __slots__ = ("account", "next", "order_id", "prev", "price", "remaining", "side")

    def __init__(self, order_id: int, account: int, side: Side, price: int, remaining: int) -> None:
        self.order_id = order_id
        self.account = account
        self.side = side
        self.price = price
        self.remaining = remaining
        self.prev: RestingOrder | None = None
        self.next: RestingOrder | None = None


class PriceLevel:
    """All resting orders at one price, oldest first."""

    __slots__ = ("count", "head", "price", "qty", "tail")

    def __init__(self, price: int) -> None:
        self.price = price
        self.qty = 0  # sum of ``remaining`` over the queue, kept for O(1) L2 depth
        self.count = 0
        self.head: RestingOrder | None = None
        self.tail: RestingOrder | None = None

    def append(self, order: RestingOrder) -> None:
        tail = self.tail
        order.prev = tail
        order.next = None
        if tail is None:
            self.head = order
        else:
            tail.next = order
        self.tail = order
        self.qty += order.remaining
        self.count += 1

    def unlink(self, order: RestingOrder) -> None:
        prev, nxt = order.prev, order.next
        if prev is None:
            self.head = nxt
        else:
            prev.next = nxt
        if nxt is None:
            self.tail = prev
        else:
            nxt.prev = prev
        order.prev = order.next = None
        self.qty -= order.remaining
        self.count -= 1

    def __iter__(self) -> Iterator[RestingOrder]:
        node = self.head
        while node is not None:
            yield node
            node = node.next


class BookSide:
    """One side of the book. See the module docstring for the key convention."""

    __slots__ = ("keys", "levels", "side", "sign")

    def __init__(self, side: Side) -> None:
        self.side = side
        self.sign = -1 if side is Side.BUY else 1
        self.levels: dict[int, PriceLevel] = {}
        self.keys: SortedList[int] = SortedList()

    def key(self, price: int) -> int:
        return price * self.sign

    def best(self) -> PriceLevel | None:
        return self.levels[self.keys[0]] if self.keys else None

    def add(self, order: RestingOrder) -> None:
        key = order.price * self.sign
        level = self.levels.get(key)
        if level is None:
            level = self.levels[key] = PriceLevel(order.price)
            self.keys.add(key)
        level.append(order)

    def remove(self, order: RestingOrder) -> None:
        key = order.price * self.sign
        level = self.levels[key]
        level.unlink(order)
        if level.head is None:
            del self.levels[key]
            self.keys.remove(key)

    def __iter__(self) -> Iterator[PriceLevel]:
        """Levels from best to worst."""
        levels = self.levels
        for key in self.keys:
            yield levels[key]


class DepthLevel(msgspec.Struct, frozen=True, array_like=True):
    """One aggregated L2 level: ``[price, qty, orders]`` on the wire."""

    price: int
    qty: int
    orders: int


class Depth(msgspec.Struct, frozen=True):
    """L2 view: price levels aggregated, best first on each side."""

    bids: list[DepthLevel]
    asks: list[DepthLevel]


class BookEntry(msgspec.Struct, frozen=True, array_like=True):
    """One L3 entry: an individual resting order, in queue position."""

    order_id: int
    account: int
    side: Side
    price: int
    qty: int


class OrderBook:
    """Both sides plus the order-id index. Knows nothing about matching rules."""

    __slots__ = ("asks", "bids", "orders")

    def __init__(self) -> None:
        self.bids = BookSide(Side.BUY)
        self.asks = BookSide(Side.SELL)
        self.orders: dict[int, RestingOrder] = {}

    def side(self, side: Side) -> BookSide:
        return self.bids if side is Side.BUY else self.asks

    def opposite(self, side: Side) -> BookSide:
        return self.asks if side is Side.BUY else self.bids

    def add(self, order: RestingOrder) -> None:
        self.orders[order.order_id] = order
        self.side(order.side).add(order)

    def remove(self, order: RestingOrder) -> None:
        del self.orders[order.order_id]
        self.side(order.side).remove(order)

    def best_bid(self) -> int | None:
        level = self.bids.best()
        return None if level is None else level.price

    def best_ask(self) -> int | None:
        level = self.asks.best()
        return None if level is None else level.price

    def depth(self, levels: int | None = None) -> Depth:
        """L2 depth, at most ``levels`` per side (all levels when ``None``)."""
        return Depth(bids=_aggregate(self.bids, levels), asks=_aggregate(self.asks, levels))

    def entries(self, side: Side | None = None) -> Iterator[BookEntry]:
        """L3 view: resting orders in priority order, bids then asks (or one side)."""
        for book_side in (self.bids, self.asks) if side is None else (self.side(side),):
            for level in book_side:
                for o in level:
                    yield BookEntry(o.order_id, o.account, o.side, o.price, o.remaining)

    def __len__(self) -> int:
        return len(self.orders)


def _aggregate(side: BookSide, limit: int | None) -> list[DepthLevel]:
    keys = side.keys if limit is None else side.keys.islice(0, limit)
    levels = side.levels
    return [DepthLevel(lvl.price, lvl.qty, lvl.count) for lvl in (levels[k] for k in keys)]
