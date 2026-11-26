"""Amend: size-down keeps queue position, anything else re-queues and may cross."""

from helpers import Submit, limit, replace

from matching_engine.domain import Command, Side
from matching_engine.engine import MatchingEngine
from matching_engine.events import (
    OrderRejected,
    OrderReplaced,
    RejectReason,
    Trade,
)

BUY, SELL = Side.BUY, Side.SELL


def _queue(engine: MatchingEngine) -> list[int]:
    return [e.order_id for e in engine.book.entries(BUY)]


def test_reducing_size_keeps_queue_position(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, BUY, 100, 5))
    submit(limit(2, BUY, 100, 5, account=2))

    events = submit(replace(1, 100, 3))

    assert events == [OrderReplaced(3, 3000, 1, 1, 100, 3, True)]
    assert _queue(engine) == [1, 2]
    assert engine.book.bids.best().qty == 8  # type: ignore[union-attr]


def test_increasing_size_loses_queue_position(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, BUY, 100, 5))
    submit(limit(2, BUY, 100, 5, account=2))

    events = submit(replace(1, 100, 8))

    assert events == [OrderReplaced(3, 3000, 1, 1, 100, 8, False)]
    assert _queue(engine) == [2, 1]


def test_changing_price_moves_the_order(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, BUY, 100, 5))

    submit(replace(1, 99, 5))

    assert engine.book.best_bid() == 99


def test_a_replace_that_crosses_trades_as_taker(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, SELL, 101, 3, account=2))
    submit(limit(2, BUY, 100, 5))

    events = submit(replace(2, 101, 5))

    assert [type(e) for e in events] == [OrderReplaced, Trade]
    assert events[1].qty == 3  # type: ignore[union-attr]
    assert [e.qty for e in engine.book.entries(BUY)] == [2]
    assert engine.book.best_ask() is None


def test_replace_rejections(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, BUY, 100, 5))
    before = engine.state_hash()

    def reason(cmd: Command) -> RejectReason:
        (e,) = submit(cmd)
        assert isinstance(e, OrderRejected)
        return e.reason

    assert reason(replace(9, 100, 5)) is RejectReason.UNKNOWN_ORDER
    assert reason(replace(1, 100, 5, account=2)) is RejectReason.NOT_ORDER_OWNER
    assert reason(replace(1, 100, 0)) is RejectReason.INVALID_QUANTITY
    assert reason(replace(1, 0, 5)) is RejectReason.INVALID_PRICE
    # only seq advanced; the book is untouched
    assert [e.qty for e in engine.book.entries()] == [5]
    assert before != engine.state_hash()  # last_seq moved
