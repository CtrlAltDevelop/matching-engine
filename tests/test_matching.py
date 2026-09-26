"""Price-time priority for limit and market orders."""

import msgspec
import pytest
from helpers import Submit, cancel, limit, market

from matching_engine.book import DepthLevel
from matching_engine.domain import NewOrder, OrderType, SequencedCommand, Side, TimeInForce
from matching_engine.engine import MatchingEngine, SequenceError
from matching_engine.events import (
    CancelReason,
    Event,
    OrderAccepted,
    OrderCancelled,
    OrderRejected,
    RejectReason,
    Trade,
)

BUY, SELL = Side.BUY, Side.SELL


def trades(events: list[Event]) -> list[Trade]:
    return [e for e in events if isinstance(e, Trade)]


def test_a_non_crossing_limit_rests_and_is_only_accepted(
    submit: Submit, engine: MatchingEngine
) -> None:
    events = submit(limit(1, BUY, 100, 5))

    assert events == [OrderAccepted(1, 1000, 1, 1, BUY, OrderType.LIMIT, TimeInForce.GTC, 100, 5)]
    assert engine.book.best_bid() == 100


def test_a_crossing_limit_trades_at_the_resting_price(submit: Submit) -> None:
    submit(limit(1, SELL, 100, 5, account=2))

    fills = trades(submit(limit(2, BUY, 103, 5)))

    assert [(t.price, t.qty, t.maker_order_id, t.taker_order_id) for t in fills] == [(100, 5, 1, 2)]


def test_the_oldest_order_at_a_price_fills_first(submit: Submit) -> None:
    submit(limit(1, SELL, 100, 3, account=2))
    submit(limit(2, SELL, 100, 3, account=3))

    fills = trades(submit(limit(3, BUY, 100, 4)))

    assert [(t.maker_order_id, t.qty, t.maker_remaining) for t in fills] == [(1, 3, 0), (2, 1, 2)]


def test_a_better_price_fills_before_an_older_order(submit: Submit) -> None:
    submit(limit(1, BUY, 99, 1, account=2))
    submit(limit(2, BUY, 100, 1, account=3))

    fills = trades(submit(limit(3, SELL, 99, 2)))

    assert [(t.maker_order_id, t.price) for t in fills] == [(2, 100), (1, 99)]


def test_a_taker_walks_levels_and_rests_the_remainder(
    submit: Submit, engine: MatchingEngine
) -> None:
    submit(limit(1, SELL, 100, 2, account=2))
    submit(limit(2, SELL, 101, 2, account=2))
    submit(limit(3, SELL, 103, 2, account=2))

    fills = trades(submit(limit(4, BUY, 101, 6)))

    assert [(t.price, t.qty, t.taker_remaining) for t in fills] == [(100, 2, 4), (101, 2, 2)]
    depth = engine.book.depth()
    assert depth.bids == [DepthLevel(101, 2, 1)]
    assert depth.asks == [DepthLevel(103, 2, 1)]


def test_trade_ids_are_consecutive_across_orders(submit: Submit) -> None:
    submit(limit(1, SELL, 100, 1, account=2))
    submit(limit(2, SELL, 100, 1, account=2))
    first = trades(submit(limit(3, BUY, 100, 1)))
    second = trades(submit(limit(4, BUY, 100, 1)))

    assert [t.trade_id for t in first + second] == [1, 2]


def test_a_market_order_sweeps_and_cancels_what_it_cannot_fill(
    submit: Submit, engine: MatchingEngine
) -> None:
    submit(limit(1, SELL, 100, 2, account=2))
    submit(limit(2, SELL, 150, 2, account=2))

    events = submit(market(3, BUY, 5))

    assert [(t.price, t.qty) for t in trades(events)] == [(100, 2), (150, 2)]
    assert events[-1] == OrderCancelled(3, 3000, 3, 1, 1, CancelReason.UNFILLED)
    assert len(engine.book) == 0


def test_a_market_order_into_an_empty_book_is_cancelled_whole(submit: Submit) -> None:
    events = submit(market(1, SELL, 4))

    assert events[-1] == OrderCancelled(1, 1000, 1, 1, 4, CancelReason.UNFILLED)


@pytest.mark.parametrize(
    ("order", "reason"),
    [
        (limit(1, BUY, 100, 0), RejectReason.INVALID_QUANTITY),
        (limit(1, BUY, 100, -3), RejectReason.INVALID_QUANTITY),
        (limit(1, BUY, 0, 1), RejectReason.INVALID_PRICE),
        (limit(1, BUY, 1 << 63, 1), RejectReason.INVALID_PRICE),
    ],
)
def test_invalid_orders_are_rejected_without_touching_the_book(
    submit: Submit, engine: MatchingEngine, order: NewOrder, reason: RejectReason
) -> None:
    events = submit(order)

    assert events == [OrderRejected(1, 1000, 1, 1, reason)]
    assert len(engine.book) == 0


def test_a_market_order_with_a_price_is_rejected(submit: Submit) -> None:
    events = submit(msgspec.structs.replace(market(1, BUY, 1), price=5))

    assert events == [OrderRejected(1, 1000, 1, 1, RejectReason.INVALID_PRICE)]


def test_a_live_order_id_cannot_be_reused(submit: Submit) -> None:
    submit(limit(1, BUY, 100, 1))

    assert submit(limit(1, BUY, 99, 1)) == [
        OrderRejected(2, 2000, 1, 1, RejectReason.DUPLICATE_ORDER_ID)
    ]


def test_cancel_removes_a_resting_order(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, BUY, 100, 5))

    assert submit(cancel(1)) == [OrderCancelled(2, 2000, 1, 1, 5, CancelReason.USER)]
    assert engine.book.best_bid() is None


def test_the_engine_refuses_a_replayed_sequence_number(engine: MatchingEngine) -> None:
    engine.process(SequencedCommand(5, 0, limit(1, BUY, 100, 1)))

    with pytest.raises(SequenceError):
        engine.process(SequencedCommand(5, 0, limit(2, BUY, 100, 1)))
