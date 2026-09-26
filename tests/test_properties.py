"""Property tests: invariants that must hold after any sequence of commands."""

from __future__ import annotations

from collections import defaultdict

from helpers import cancel, limit, market
from hypothesis import given, settings
from hypothesis import strategies as st

from matching_engine.domain import (
    Command,
    NewOrder,
    OrderType,
    SelfTradePrevention,
    SequencedCommand,
    Side,
    TimeInForce,
)
from matching_engine.engine import MatchingEngine
from matching_engine.events import Event, OrderAccepted, OrderCancelled, Trade

ACCOUNTS = st.integers(1, 3)  # few accounts, so self-trade prevention actually fires


@st.composite
def order_flows(draw: st.DrawFn) -> list[Command]:
    """Random but plausible flow: prices in a narrow band so orders cross often."""
    commands: list[Command] = []
    for order_id in range(1, draw(st.integers(1, 150)) + 1):
        if order_id > 1 and draw(st.integers(0, 3)) == 0:
            commands.append(cancel(draw(st.integers(1, order_id - 1)), draw(ACCOUNTS)))
            continue
        side = draw(st.sampled_from(Side))
        qty = draw(st.integers(1, 20))
        account = draw(ACCOUNTS)
        stp = draw(st.sampled_from(SelfTradePrevention))
        if draw(st.integers(0, 5)) == 0:
            tif = draw(st.sampled_from([TimeInForce.IOC, TimeInForce.FOK]))
            commands.append(market(order_id, side, qty, account=account, tif=tif, stp=stp))
            continue
        tif = draw(st.sampled_from(TimeInForce))
        post_only = tif is TimeInForce.GTC and draw(st.booleans())
        price = draw(st.integers(95, 105))
        commands.append(
            limit(
                order_id, side, price, qty, account=account, tif=tif, post_only=post_only, stp=stp
            )
        )
    return commands


def run(engine: MatchingEngine, commands: list[Command], first_seq: int = 1) -> list[Event]:
    events: list[Event] = []
    for seq, cmd in enumerate(commands, start=first_seq):
        events += engine.process(SequencedCommand(seq, seq, cmd))
    return events


def assert_book_is_consistent(engine: MatchingEngine) -> None:
    book = engine.book
    bid, ask = book.best_bid(), book.best_ask()
    assert bid is None or ask is None or bid < ask, "book is crossed"
    indexed = 0
    for side in (book.bids, book.asks):
        assert list(side.keys) == sorted(side.levels), "keys and levels disagree"
        for level in side:
            orders = list(level)
            assert orders, "an empty level was left behind"
            assert level.qty == sum(o.remaining for o in orders)
            assert level.count == len(orders)
            assert all(o.remaining > 0 and o.price == level.price for o in orders)
            assert all(book.orders[o.order_id] is o for o in orders)
            indexed += len(orders)
    assert indexed == len(book.orders)


@settings(max_examples=200, deadline=None)
@given(order_flows())
def test_the_book_is_never_crossed_and_stays_internally_consistent(
    commands: list[Command],
) -> None:
    engine = MatchingEngine()
    for seq, cmd in enumerate(commands, start=1):
        engine.process(SequencedCommand(seq, seq, cmd))
        assert_book_is_consistent(engine)


@settings(max_examples=200, deadline=None)
@given(order_flows())
def test_every_accepted_lot_is_filled_cancelled_or_still_resting(
    commands: list[Command],
) -> None:
    engine = MatchingEngine()
    events = run(engine, commands)

    orders = {c.order_id: c for c in commands if isinstance(c, NewOrder)}
    open_qty: dict[int, int] = defaultdict(int)
    for e in events:
        if isinstance(e, OrderAccepted):
            open_qty[e.order_id] += e.qty
        elif isinstance(e, Trade):
            assert e.qty > 0
            open_qty[e.maker_order_id] -= e.qty
            open_qty[e.taker_order_id] -= e.qty
            taker = orders[e.taker_order_id]
            if taker.order_type is OrderType.LIMIT:  # never worse than the taker's limit
                assert e.price <= taker.price if taker.side is Side.BUY else e.price >= taker.price
        elif isinstance(e, OrderCancelled):
            open_qty[e.order_id] -= e.remaining

    resting = {oid: o.remaining for oid, o in engine.book.orders.items()}
    assert {oid: q for oid, q in open_qty.items() if q} == resting


@settings(max_examples=150, deadline=None)
@given(order_flows(), st.data())
def test_restoring_exported_state_mid_flow_changes_nothing(
    commands: list[Command], data: st.DataObject
) -> None:
    split = data.draw(st.integers(0, len(commands)))
    straight = MatchingEngine()
    expected = run(straight, commands)

    first = MatchingEngine()
    head = run(first, commands[:split])
    resumed = MatchingEngine.restore(first.export_state())
    assert resumed.state_hash() == first.state_hash()
    tail = run(resumed, commands[split:], first_seq=split + 1)

    assert head + tail == expected
    assert resumed.state_hash() == straight.state_hash()


def test_state_hash_sees_queue_order_not_just_totals() -> None:
    a, b = MatchingEngine(), MatchingEngine()
    run(a, [limit(1, Side.BUY, 100, 1, account=1), limit(2, Side.BUY, 100, 1, account=2)])
    run(b, [limit(2, Side.BUY, 100, 1, account=2), limit(1, Side.BUY, 100, 1, account=1)])

    assert a.book.depth() == b.book.depth()
    assert a.state_hash() != b.state_hash()
