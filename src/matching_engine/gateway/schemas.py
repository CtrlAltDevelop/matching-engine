"""Wire formats: request validation and rendering integers back into decimals.

Prices and quantities cross the wire as decimal *strings* in both directions.
JSON numbers are binary floats in most client libraries, and a price that
arrives as ``0.30000000000000004`` is exactly the bug integer ticks exist to
prevent.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

import msgspec
from pydantic import BaseModel, ConfigDict, Field

from matching_engine.book import Depth
from matching_engine.domain import (
    MAX_INT64,
    NewOrder,
    OrderType,
    SelfTradePrevention,
    Side,
    TimeInForce,
)
from matching_engine.events import Event, OrderCancelled, OrderRejected, Trade
from matching_engine.market import MarketSpec

_PRICE_FIELDS = frozenset({"price"})
_QTY_FIELDS = frozenset({"qty", "remaining", "maker_remaining", "taker_remaining"})

OrderStatus = Literal["resting", "partially_filled", "filled", "cancelled", "rejected"]


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: int = Field(ge=1, le=MAX_INT64)
    side: Side
    type: OrderType = OrderType.LIMIT
    price: Decimal | None = Field(default=None, description="Required for limit orders")
    quantity: Decimal
    time_in_force: TimeInForce | None = Field(
        default=None, description="Defaults to gtc for limit orders and ioc for market orders"
    )
    post_only: bool = False
    self_trade_prevention: SelfTradePrevention = SelfTradePrevention.CANCEL_TAKER

    def to_command(self, spec: MarketSpec, order_id: int) -> NewOrder:
        """Convert to engine units. Raises ``ValueError`` for off-grid or missing values."""
        if self.type is OrderType.LIMIT:
            if self.price is None:
                raise ValueError("a limit order needs a price")
            price = spec.to_ticks(self.price)
            tif = self.time_in_force or TimeInForce.GTC
        else:
            if self.price is not None:
                raise ValueError("a market order takes no price")
            price = 0
            tif = self.time_in_force or TimeInForce.IOC
        return NewOrder(
            order_id,
            self.account,
            self.side,
            price,
            spec.to_lots(self.quantity),
            self.type,
            tif,
            self.post_only,
            self.self_trade_prevention,
        )


class OrderResponse(BaseModel):
    order_id: int
    seq: int
    status: OrderStatus
    filled_quantity: str
    events: list[dict[str, Any]]


class CancelResponse(BaseModel):
    order_id: int
    seq: int
    status: Literal["cancelled", "rejected"]
    events: list[dict[str, Any]]


class MarketInfo(BaseModel):
    symbol: str
    tick_size: str
    lot_size: str
    last_seq: int
    best_bid: str | None
    best_ask: str | None


class DepthResponse(BaseModel):
    symbol: str
    seq: int
    bids: list[tuple[str, str, int]]
    asks: list[tuple[str, str, int]]


class BookOrder(BaseModel):
    order_id: int
    side: Side
    price: str
    quantity: str


class BookResponse(BaseModel):
    symbol: str
    seq: int
    orders: list[BookOrder]


def render_event(spec: MarketSpec, event: Event) -> dict[str, Any]:
    """An event as a JSON-ready dict, integers converted back to decimal strings."""
    out: dict[str, Any] = {"type": type(event).__struct_config__.tag}
    for name, value in msgspec.structs.asdict(event).items():
        if name in _PRICE_FIELDS:
            out[name] = spec.price(value)
        elif name in _QTY_FIELDS:
            out[name] = spec.qty(value)
        else:
            out[name] = value
    return out


def render_depth(spec: MarketSpec, seq: int, depth: Depth) -> DepthResponse:
    def side(levels: list[Any]) -> list[tuple[str, str, int]]:
        return [(spec.price(lvl.price), spec.qty(lvl.qty), lvl.orders) for lvl in levels]

    return DepthResponse(symbol=spec.symbol, seq=seq, bids=side(depth.bids), asks=side(depth.asks))


def order_outcome(order: NewOrder, events: list[Event]) -> tuple[OrderStatus, int]:
    """Summarize what happened to a new order: its status and filled lots."""
    filled = 0
    status: OrderStatus | None = None
    for e in events:
        if isinstance(e, OrderRejected):
            return "rejected", 0
        if isinstance(e, Trade) and e.taker_order_id == order.order_id:
            filled += e.qty
        elif isinstance(e, OrderCancelled) and e.order_id == order.order_id:
            status = "cancelled"
    if status is None:
        status = "filled" if filled == order.qty else "partially_filled" if filled else "resting"
    return status, filled
