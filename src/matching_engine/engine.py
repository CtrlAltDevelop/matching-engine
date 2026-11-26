"""The matching engine for one market.

Single-threaded and deterministic by construction: no clock, no randomness,
no I/O. Its only input is a :class:`SequencedCommand` and its only output is
the list of events that command caused, so the same log replayed into a fresh
engine produces the same events and the same book, every time.
"""

from __future__ import annotations

import hashlib
import struct

import msgspec

from matching_engine.book import BookEntry, BookSide, OrderBook, RestingOrder
from matching_engine.domain import (
    MAX_INT64,
    CancelOrder,
    NewOrder,
    OrderType,
    ReplaceOrder,
    SelfTradePrevention,
    SequencedCommand,
    Side,
    TimeInForce,
)
from matching_engine.events import (
    CancelReason,
    Event,
    OrderAccepted,
    OrderCancelled,
    OrderRejected,
    OrderReplaced,
    OrderTriggered,
    RejectReason,
    Trade,
)

# A market order crosses every level: compare its "limit key" as +infinity.
_NO_LIMIT = MAX_INT64


_HASH_COUNTERS = struct.Struct("<QQq")
_HASH_ORDER = struct.Struct("<QQ?qq")
_TIF_ORDER = tuple(TimeInForce)
_STP_ORDER = tuple(SelfTradePrevention)
_HASH_STOP = struct.Struct("<QQ?qqqBBB")


class SequenceError(ValueError):
    """A command arrived with a sequence number that is not the next one."""


class EngineState(msgspec.Struct, frozen=True):
    """Everything needed to rebuild an engine: counters plus the book in priority order."""

    last_seq: int
    next_trade_id: int
    orders: list[BookEntry]
    last_trade_price: int = 0  # 0: nothing has traded yet, so no stop can trigger
    stops: list[NewOrder] = []  # parked stop orders, in arrival order


class MatchingEngine:
    __slots__ = ("book", "last_seq", "last_trade_price", "next_trade_id", "stops")

    def __init__(self) -> None:
        self.book = OrderBook()
        self.last_seq = 0
        self.next_trade_id = 1
        self.last_trade_price = 0
        # Parked stops in arrival order (dict order), keyed by order id. Triggered in that
        # order. ponytail: every trade scans all stops, O(stops); index by stop price if
        # parked stops ever number in the thousands.
        self.stops: dict[int, NewOrder] = {}

    @classmethod
    def restore(cls, state: EngineState) -> MatchingEngine:
        """Rebuild an engine from :meth:`export_state`, queue positions included.

        Entries arrive best level first and oldest first within a level, so
        appending them in order recreates every FIFO queue exactly.
        """
        engine = cls()
        engine.last_seq = state.last_seq
        engine.next_trade_id = state.next_trade_id
        engine.last_trade_price = state.last_trade_price
        engine.stops = {o.order_id: o for o in state.stops}
        add = engine.book.add
        for e in state.orders:
            add(RestingOrder(e.order_id, e.account, e.side, e.price, e.qty))
        return engine

    def export_state(self) -> EngineState:
        return EngineState(
            self.last_seq,
            self.next_trade_id,
            list(self.book.entries()),
            self.last_trade_price,
            list(self.stops.values()),
        )

    def state_hash(self) -> str:
        """A fingerprint of the full engine state, queue order included.

        Two engines with the same hash hold the same orders at the same queue
        positions and will react identically to any future command, which is
        what the replay and recovery tests compare.
        """
        digest = hashlib.blake2b(
            _HASH_COUNTERS.pack(self.last_seq, self.next_trade_id, self.last_trade_price),
            digest_size=32,
        )
        pack = _HASH_ORDER.pack
        for e in self.book.entries():
            digest.update(pack(e.order_id, e.account, e.side is Side.BUY, e.price, e.qty))
        for o in self.stops.values():
            digest.update(
                _HASH_STOP.pack(
                    o.order_id,
                    o.account,
                    o.side is Side.BUY,
                    o.stop_price,
                    o.price,
                    o.qty,
                    o.order_type is OrderType.MARKET,
                    _TIF_ORDER.index(o.tif),
                    _STP_ORDER.index(o.stp),
                )
            )
        return digest.hexdigest()

    def process(self, sc: SequencedCommand) -> list[Event]:
        """Apply one command and return everything it caused, in order."""
        seq = sc.seq
        if seq <= self.last_seq:
            raise SequenceError(f"seq {seq} is not after {self.last_seq}")
        self.last_seq = seq
        cmd = sc.command
        if type(cmd) is NewOrder:
            events = (
                self._park_stop(cmd, seq, sc.ts)
                if cmd.stop_price
                else self._new_order(cmd, seq, sc.ts)
            )
        elif type(cmd) is CancelOrder:
            events = self._cancel(cmd, seq, sc.ts)
        else:
            assert type(cmd) is ReplaceOrder
            events = self._replace(cmd, seq, sc.ts)
        if self.stops:
            self._trigger_stops(events, seq, sc.ts)
        return events

    def _park_stop(self, o: NewOrder, seq: int, ts: int) -> list[Event]:
        reason = self._validate(o)
        if reason is None and o.post_only:  # a stop is a taker when it fires
            reason = RejectReason.INVALID_TIME_IN_FORCE
        if reason is not None:
            return [OrderRejected(seq, ts, o.order_id, o.account, reason)]
        self.stops[o.order_id] = o
        return [
            OrderAccepted(
                seq,
                ts,
                o.order_id,
                o.account,
                o.side,
                o.order_type,
                o.tif,
                o.price,
                o.qty,
                o.stop_price,
            )
        ]

    def _stop_hit(self, o: NewOrder) -> bool:
        last = self.last_trade_price
        if not last:
            return False
        return last >= o.stop_price if o.side is Side.BUY else last <= o.stop_price

    def _trigger_stops(self, events: list[Event], seq: int, ts: int) -> None:
        """Submit every stop the last trade price has reached, until none are left.

        Stops fire in arrival order. A fired stop can trade, move the last
        price and hit more stops, so this loops; each pass removes at least
        one stop, so it terminates.
        """
        while True:
            hit = [o for o in self.stops.values() if self._stop_hit(o)]
            if not hit:
                return
            for o in hit:
                del self.stops[o.order_id]
                events.append(OrderTriggered(seq, ts, o.order_id, o.account))
                events.extend(
                    self._new_order(
                        msgspec.structs.replace(o, stop_price=0), seq, ts, announce=False
                    )
                )

    def _new_order(self, o: NewOrder, seq: int, ts: int, *, announce: bool = True) -> list[Event]:
        reason = self._validate(o)
        if reason is not None:
            return [OrderRejected(seq, ts, o.order_id, o.account, reason)]

        opposite = self.book.opposite(o.side)
        limit_key = _NO_LIMIT if o.order_type is OrderType.MARKET else opposite.key(o.price)
        if o.post_only:
            if opposite.keys and opposite.keys[0] <= limit_key:
                reason = RejectReason.POST_ONLY_WOULD_CROSS
        elif o.tif is TimeInForce.FOK and not self._can_fill(o, opposite, limit_key):
            reason = RejectReason.FOK_NOT_FILLABLE
        if reason is not None:
            return [OrderRejected(seq, ts, o.order_id, o.account, reason)]

        events: list[Event] = []
        if announce:
            events.append(
                OrderAccepted(
                    seq, ts, o.order_id, o.account, o.side, o.order_type, o.tif, o.price, o.qty
                )
            )
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
        if not 0 <= o.stop_price <= MAX_INT64:
            return RejectReason.INVALID_PRICE
        if o.post_only and o.tif is not TimeInForce.GTC:  # a maker-only order must rest
            return RejectReason.INVALID_TIME_IN_FORCE
        if o.order_id in self.book.orders or o.order_id in self.stops:
            return RejectReason.DUPLICATE_ORDER_ID
        return None

    def _can_fill(self, o: NewOrder, opposite: BookSide, limit_key: int) -> bool:
        """Whether ``o`` would fill completely right now. Reads, never mutates.

        FOK must be all-or-nothing, so it is decided before the first fill
        rather than by unwinding trades afterwards. The walk mirrors what
        ``_match`` would do with the same order's self-trade policy: an own
        order either stops matching or is skipped (it would be cancelled).
        """
        needed = o.qty
        stp, account = o.stp, o.account
        levels = opposite.levels
        for key in opposite.keys:
            if key > limit_key:
                return False
            level = levels[key]
            if stp is SelfTradePrevention.NONE:
                needed -= level.qty
            else:
                for maker in level:
                    if maker.account != account:
                        needed -= maker.remaining
                    elif stp is not SelfTradePrevention.CANCEL_MAKER:
                        return False
                    if needed <= 0:
                        return True
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
        """Cross ``o`` against the opposite side; return what is left to handle.

        Always takes the head of the best level: price priority from the
        sorted keys, time priority from the FIFO queue. Fills print at the
        maker's price, so an aggressive limit gets price improvement.

        When self-trade prevention cancels the taker, the cancel event is
        emitted here and 0 is returned: there is nothing left to rest.
        """
        book = self.book
        keys, levels = opposite.keys, opposite.levels
        account, stp = o.account, o.stp
        remaining = o.qty
        while remaining and keys:
            key = keys[0]
            if key > limit_key:
                break
            level = levels[key]
            maker = level.head
            assert maker is not None  # empty levels are removed eagerly
            if maker.account == account and stp is not SelfTradePrevention.NONE:
                if stp is not SelfTradePrevention.CANCEL_TAKER:
                    book.remove(maker)
                    events.append(
                        OrderCancelled(
                            seq,
                            ts,
                            maker.order_id,
                            maker.account,
                            maker.remaining,
                            CancelReason.SELF_TRADE,
                        )
                    )
                    if stp is SelfTradePrevention.CANCEL_MAKER:
                        continue
                events.append(
                    OrderCancelled(seq, ts, o.order_id, account, remaining, CancelReason.SELF_TRADE)
                )
                return 0

            fill = remaining if remaining < maker.remaining else maker.remaining
            remaining -= fill
            maker.remaining -= fill
            level.qty -= fill
            trade_id = self.next_trade_id
            self.next_trade_id = trade_id + 1
            self.last_trade_price = level.price
            events.append(
                Trade(
                    seq,
                    ts,
                    trade_id,
                    level.price,
                    fill,
                    o.side,
                    o.order_id,
                    account,
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
        parked = self.stops.get(c.order_id)
        if parked is not None:
            if parked.account != c.account:
                return [OrderRejected(seq, ts, c.order_id, c.account, RejectReason.NOT_ORDER_OWNER)]
            del self.stops[c.order_id]
            return [
                OrderCancelled(
                    seq, ts, parked.order_id, parked.account, parked.qty, CancelReason.USER
                )
            ]
        order = self.book.orders.get(c.order_id)
        if order is None:
            return [OrderRejected(seq, ts, c.order_id, c.account, RejectReason.UNKNOWN_ORDER)]
        if order.account != c.account:
            return [OrderRejected(seq, ts, c.order_id, c.account, RejectReason.NOT_ORDER_OWNER)]
        self.book.remove(order)
        return [
            OrderCancelled(
                seq, ts, order.order_id, order.account, order.remaining, CancelReason.USER
            )
        ]

    def _replace(self, r: ReplaceOrder, seq: int, ts: int) -> list[Event]:
        order = self.book.orders.get(r.order_id)
        reason = None
        if order is None:
            reason = RejectReason.UNKNOWN_ORDER
        elif order.account != r.account:
            reason = RejectReason.NOT_ORDER_OWNER
        elif not 0 < r.qty <= MAX_INT64:
            reason = RejectReason.INVALID_QUANTITY
        elif not 0 < r.price <= MAX_INT64:
            reason = RejectReason.INVALID_PRICE
        if reason is not None or order is None:
            return [
                OrderRejected(seq, ts, r.order_id, r.account, reason or RejectReason.UNKNOWN_ORDER)
            ]

        if r.price == order.price and r.qty <= order.remaining:
            self.book.reduce(order, r.qty)
            return [OrderReplaced(seq, ts, r.order_id, r.account, r.price, r.qty, True)]

        self.book.remove(order)
        events: list[Event] = [OrderReplaced(seq, ts, r.order_id, r.account, r.price, r.qty, False)]
        o = NewOrder(r.order_id, r.account, order.side, r.price, r.qty)
        opposite = self.book.opposite(o.side)
        remaining = self._match(o, opposite, opposite.key(o.price), events, seq, ts)
        if remaining:
            self.book.add(RestingOrder(o.order_id, o.account, o.side, o.price, remaining))
        return events
