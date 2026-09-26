"""Terse command builders shared by the test modules."""

from __future__ import annotations

from collections.abc import Callable

from matching_engine.domain import (
    CancelOrder,
    Command,
    NewOrder,
    OrderType,
    SelfTradePrevention,
    Side,
    TimeInForce,
)
from matching_engine.events import Event

type Submit = Callable[[Command], list[Event]]


def limit(
    order_id: int,
    side: Side,
    price: int,
    qty: int,
    *,
    account: int = 1,
    tif: TimeInForce = TimeInForce.GTC,
    post_only: bool = False,
    stp: SelfTradePrevention = SelfTradePrevention.CANCEL_TAKER,
) -> NewOrder:
    return NewOrder(order_id, account, side, price, qty, OrderType.LIMIT, tif, post_only, stp)


def market(
    order_id: int,
    side: Side,
    qty: int,
    *,
    account: int = 1,
    tif: TimeInForce = TimeInForce.IOC,
    stp: SelfTradePrevention = SelfTradePrevention.CANCEL_TAKER,
) -> NewOrder:
    return NewOrder(order_id, account, side, 0, qty, OrderType.MARKET, tif, False, stp)


def cancel(order_id: int, account: int = 1) -> CancelOrder:
    return CancelOrder(order_id, account)
