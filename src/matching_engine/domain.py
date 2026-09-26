"""Order-entry vocabulary: the enums and commands the engine accepts.

Prices are integer *ticks* and quantities integer *lots*. Converting from the
human decimal form happens once, at the gateway boundary (see ``market.py``);
nothing past that point ever sees a float or a ``Decimal``.

Commands are frozen ``msgspec`` structs: immutable like a frozen dataclass,
but several times cheaper to construct, and they encode straight to JSON or
MessagePack without a hand-written serializer.
"""

from __future__ import annotations

from enum import StrEnum

import msgspec

# Quantities and prices are packed as signed 64-bit integers in the WAL.
MAX_INT64 = (1 << 63) - 1


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class TimeInForce(StrEnum):
    GTC = "gtc"  # good till cancelled: the remainder rests on the book
    IOC = "ioc"  # immediate or cancel: fill what crosses, cancel the rest
    FOK = "fok"  # fill or kill: fill completely at once, or not at all


class SelfTradePrevention(StrEnum):
    """What happens when an incoming order would trade with its own account."""

    NONE = "none"  # allow the self-trade
    CANCEL_TAKER = "cancel_taker"  # cancel the incoming remainder, keep the resting order
    CANCEL_MAKER = "cancel_maker"  # cancel the resting order and keep matching
    CANCEL_BOTH = "cancel_both"  # cancel both


class NewOrder(msgspec.Struct, frozen=True, gc=False, tag="new"):
    """Place an order. ``price`` is ignored (and must be 0) for market orders."""

    order_id: int
    account: int
    side: Side
    price: int
    qty: int
    order_type: OrderType = OrderType.LIMIT
    tif: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    stp: SelfTradePrevention = SelfTradePrevention.CANCEL_TAKER


class CancelOrder(msgspec.Struct, frozen=True, gc=False, tag="cancel"):
    """Cancel a resting order. Only the account that placed it may cancel it."""

    order_id: int
    account: int


type Command = NewOrder | CancelOrder


class SequencedCommand(msgspec.Struct, frozen=True, gc=False):
    """A command stamped by the sequencer: the unit written to the WAL.

    ``seq`` is gap-free and strictly increasing per market; ``ts`` is wall-clock
    nanoseconds taken by the sequencer, never by the engine, so replaying the
    log reproduces every event bit for bit.
    """

    seq: int
    ts: int
    command: Command
