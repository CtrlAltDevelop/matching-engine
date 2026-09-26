"""The engine's output: an ordered stream of immutable events.

Every event carries the ``seq`` and ``ts`` of the command that caused it, so a
consumer can line events up with the WAL and deduplicate after a reconnect.
The event list returned for one command is the complete, ordered effect of
that command; nothing about the book changes that is not in it.
"""

from __future__ import annotations

from enum import StrEnum

import msgspec

from matching_engine.domain import OrderType, Side, TimeInForce


class RejectReason(StrEnum):
    INVALID_QUANTITY = "invalid_quantity"
    INVALID_PRICE = "invalid_price"
    INVALID_TIME_IN_FORCE = "invalid_time_in_force"
    DUPLICATE_ORDER_ID = "duplicate_order_id"
    FOK_NOT_FILLABLE = "fok_not_fillable"
    POST_ONLY_WOULD_CROSS = "post_only_would_cross"
    UNKNOWN_ORDER = "unknown_order"
    NOT_ORDER_OWNER = "not_order_owner"


class CancelReason(StrEnum):
    USER = "user"  # an explicit CancelOrder
    UNFILLED = "unfilled"  # IOC or market remainder with nothing left to cross
    SELF_TRADE = "self_trade"  # removed by self-trade prevention


class OrderAccepted(msgspec.Struct, frozen=True, gc=False, tag="order_accepted"):
    seq: int
    ts: int
    order_id: int
    account: int
    side: Side
    order_type: OrderType
    tif: TimeInForce
    price: int
    qty: int


class Trade(msgspec.Struct, frozen=True, gc=False, tag="trade"):
    """A fill. Always at the maker's (resting) price."""

    seq: int
    ts: int
    trade_id: int
    price: int
    qty: int
    taker_side: Side
    taker_order_id: int
    taker_account: int
    maker_order_id: int
    maker_account: int
    maker_remaining: int
    taker_remaining: int


class OrderCancelled(msgspec.Struct, frozen=True, gc=False, tag="order_cancelled"):
    seq: int
    ts: int
    order_id: int
    account: int
    remaining: int
    reason: CancelReason


class OrderRejected(msgspec.Struct, frozen=True, gc=False, tag="order_rejected"):
    seq: int
    ts: int
    order_id: int
    account: int
    reason: RejectReason


type Event = OrderAccepted | Trade | OrderCancelled | OrderRejected
