"""Synthetic, reproducible order flow for benchmarks, soak runs and crash tests.

The mix is roughly what a liquid market looks like from the engine's side:
mostly passive limit orders near the touch, a steady stream of cancels, and a
minority of aggressive orders that cross. The randomness lives here, in the
load generator, never in the engine: the same seed always yields the same
commands, so a run can be replayed and compared by state hash.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

from matching_engine.domain import (
    CancelOrder,
    Command,
    NewOrder,
    OrderType,
    Side,
    TimeInForce,
)

CANCEL_SHARE = 0.25
MARKET_SHARE = 0.05


def order_flow(
    seed: int,
    count: int,
    *,
    accounts: int = 1_000,
    mid: int = 100_000,
    depth_ticks: int = 50,
    cross_ticks: int = 5,
) -> Iterator[Command]:
    """Yield ``count`` commands; command ``i`` (1-based) uses order id ``i``.

    Limit prices land within ``depth_ticks`` of ``mid`` on the passive side and
    up to ``cross_ticks`` through it, so about one limit in ten takes liquidity.
    Cancels target a random earlier order with its owner's account; some of
    those orders will have filled already, so a realistic share of cancels is
    rejected as unknown.
    """
    rng = random.Random(seed)
    placed: list[tuple[int, int]] = []  # (order_id, account) of every limit so far
    for order_id in range(1, count + 1):
        roll = rng.random()
        if roll < CANCEL_SHARE and placed:
            victim, owner = placed[rng.randrange(len(placed))]
            yield CancelOrder(victim, owner)
            continue
        side = Side.BUY if rng.random() < 0.5 else Side.SELL
        account = rng.randint(1, accounts)
        qty = rng.randint(1, 100)
        if roll < CANCEL_SHARE + MARKET_SHARE:
            yield NewOrder(order_id, account, side, 0, qty, OrderType.MARKET, TimeInForce.IOC)
            continue
        offset = rng.randint(-cross_ticks, depth_ticks)
        price = mid - offset if side is Side.BUY else mid + offset
        placed.append((order_id, account))
        yield NewOrder(order_id, account, side, price, qty)
