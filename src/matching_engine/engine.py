"""The matching engine for one market.

Single-threaded and deterministic by construction: no clock, no randomness,
no I/O. Its only input is a :class:`SequencedCommand` and its only output is
the list of events that command caused, so the same log replayed into a fresh
engine produces the same events and the same book, every time.
"""

from __future__ import annotations

from matching_engine.book import BookSide, OrderBook, RestingOrder
from matching_engine.domain import (
    MAX_INT64,
    CancelOrder,
    NewOrder,
    OrderType,
    SequencedCommand,
    TimeInForce,
)
from matching_engine.events import (
    CancelReason,
    Event,
    OrderAccepted,
    OrderCancelled,
    OrderRejected,
    RejectReason,
    Trade,
)

# A market order crosses every level: compare its "limit key" as +infinity.
_NO_LIMIT = MAX_INT64


class SequenceError(ValueError):
    """A command arrived with a sequence number that is not the next one."""


class MatchingEngine:
    __slots__ = ("book", "last_seq", "next_trade_id")

    def __init__(self) -> None:
        self.book = OrderBook()
        self.last_seq = 0
        self.next_trade_id = 1

    def process(self, sc: SequencedCommand) -> list[Event]:
        """Apply one command and return everything it caused, in order."""
        seq = sc.seq
        if seq <= self.last_seq:
            raise SequenceError(f"seq {seq} is not after {self.last_seq}")
        self.last_seq = seq
        cmd = sc.command
        if type(cmd) is NewOrder:
            return self._new_order(cmd, seq, sc.ts)
        assert type(cmd) is CancelOrder
        return self._cancel(cmd, seq, sc.ts)

    def _new_order(self, o: NewOrder, seq: int, ts: int) -> list[Event]:
        reason = self._validate(o)
        if reason is not None:
            return [OrderRejected(seq, ts, o.order_id, o.account, reason)]

        opposite = self.book.opposite(o.side)
        limit_key = _NO_LIMIT if o.order_type is OrderType.MARKET else opposite.key(o.price)
        if o.tif is TimeInForce.FOK and not self._can_fill(o, opposite, limit_key):
            return [OrderRejected(seq, ts, o.order_id, o.account, RejectReason.FOK_NOT_FILLABLE)]

        events: list[Event] = [
            OrderAccepted(
                seq, ts, o.order_id, o.account, o.side, o.order_type, o.tif, o.price, o.qty
            )
        ]
        remaining = self._match(o, opposite, limit_key, events, seq, ts)
        if remaining:
            if o.order_type is OrderType.LIMIT and o.tif is TimeInForce.GTC:
                self.book.add(RestingOrder(o.order_id, o.account, o.side, o.price, remaining))
            else:
                events.append(
                    OrderCancelled(seq, ts, o.order_id, o.account, remaining, CancelReason.UNFILLED)
                )
        return events

    def _validate(self, o: NewOrder) -> RejectReason | None:
        if not 0 < o.qty <= MAX_INT64:
            return RejectReason.INVALID_QUANTITY
        if o.order_type is OrderType.MARKET:
            if o.price != 0:
                return RejectReason.INVALID_PRICE
            if o.tif is TimeInForce.GTC:  # nothing to rest at: market means IOC or FOK
                return RejectReason.INVALID_TIME_IN_FORCE
        elif not 0 < o.price <= MAX_INT64:
            return RejectReason.INVALID_PRICE
        if o.order_id in self.book.orders:
            return RejectReason.DUPLICATE_ORDER_ID
        return None

    def _can_fill(self, o: NewOrder, opposite: BookSide, limit_key: int) -> bool:
        """Whether ``o`` would fill completely right now. Reads, never mutates.

        FOK must be all-or-nothing, so it is decided before the first fill
        rather than by unwinding trades afterwards.
        """
        needed = o.qty
        levels = opposite.levels
        for key in opposite.keys:
            if key > limit_key:
                return False
            needed -= levels[key].qty
            if needed <= 0:
                return True
        return False

    def _match(
        self,
        o: NewOrder,
        opposite: BookSide,
        limit_key: int,
        events: list[Event],
        seq: int,
        ts: int,
    ) -> int:
        """Cross ``o`` against the opposite side; return the unfilled quantity.

        Always takes the head of the best level: price priority from the
        sorted keys, time priority from the FIFO queue. Fills print at the
        maker's price, so an aggressive limit gets price improvement.
        """
        book = self.book
        keys, levels = opposite.keys, opposite.levels
        remaining = o.qty
        while remaining and keys:
            key = keys[0]
            if key > limit_key:
                break
            level = levels[key]
            maker = level.head
            assert maker is not None  # empty levels are removed eagerly
            fill = remaining if remaining < maker.remaining else maker.remaining
            remaining -= fill
            maker.remaining -= fill
            level.qty -= fill
            trade_id = self.next_trade_id
            self.next_trade_id = trade_id + 1
            events.append(
                Trade(
                    seq,
                    ts,
                    trade_id,
                    level.price,
                    fill,
                    o.side,
                    o.order_id,
                    o.account,
                    maker.order_id,
                    maker.account,
                    maker.remaining,
                    remaining,
                )
            )
            if maker.remaining == 0:
                book.remove(maker)
        return remaining

    def _cancel(self, c: CancelOrder, seq: int, ts: int) -> list[Event]:
        order = self.book.orders.get(c.order_id)
        if order is None:
            return [OrderRejected(seq, ts, c.order_id, c.account, RejectReason.UNKNOWN_ORDER)]
        self.book.remove(order)
        return [
            OrderCancelled(
                seq, ts, order.order_id, order.account, order.remaining, CancelReason.USER
            )
        ]
