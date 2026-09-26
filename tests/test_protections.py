"""Cancel ownership, post-only and self-trade prevention."""

import pytest
from helpers import Submit, cancel, limit, market

from matching_engine.book import DepthLevel
from matching_engine.domain import SelfTradePrevention, Side, TimeInForce
from matching_engine.engine import MatchingEngine
from matching_engine.events import (
    CancelReason,
    OrderAccepted,
    OrderCancelled,
    OrderRejected,
    RejectReason,
    Trade,
)

BUY, SELL = Side.BUY, Side.SELL
STP = SelfTradePrevention


def test_cancel_after_a_partial_fill_cancels_only_the_remainder(submit: Submit) -> None:
    submit(limit(1, SELL, 100, 10, account=2))
    submit(limit(2, BUY, 100, 4))

    assert submit(cancel(1, account=2)) == [OrderCancelled(3, 3000, 1, 2, 6, CancelReason.USER)]


def test_cancelling_an_unknown_or_finished_order_is_rejected(submit: Submit) -> None:
    submit(limit(1, SELL, 100, 1, account=2))
    submit(limit(2, BUY, 100, 1))

    assert submit(cancel(1, account=2)) == [
        OrderRejected(3, 3000, 1, 2, RejectReason.UNKNOWN_ORDER)
    ]


def test_only_the_owner_can_cancel(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, BUY, 100, 1, account=7))

    assert submit(cancel(1, account=8)) == [
        OrderRejected(2, 2000, 1, 8, RejectReason.NOT_ORDER_OWNER)
    ]
    assert len(engine.book) == 1


def test_post_only_rests_when_it_does_not_cross(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, SELL, 101, 1, account=2))

    events = submit(limit(2, BUY, 100, 1, post_only=True))

    assert [type(e) for e in events] == [OrderAccepted]
    assert engine.book.best_bid() == 100


def test_post_only_that_would_take_is_rejected(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, SELL, 100, 1, account=2))

    assert submit(limit(2, BUY, 100, 1, post_only=True)) == [
        OrderRejected(2, 2000, 2, 1, RejectReason.POST_ONLY_WOULD_CROSS)
    ]
    assert engine.book.depth().asks == [DepthLevel(100, 1, 1)]


@pytest.mark.parametrize("tif", [TimeInForce.IOC, TimeInForce.FOK])
def test_post_only_must_be_good_till_cancelled(submit: Submit, tif: TimeInForce) -> None:
    assert submit(limit(1, BUY, 100, 1, post_only=True, tif=tif)) == [
        OrderRejected(1, 1000, 1, 1, RejectReason.INVALID_TIME_IN_FORCE)
    ]


def test_stp_cancel_taker_keeps_the_resting_order(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, SELL, 100, 2, account=2))
    submit(limit(2, SELL, 100, 5, account=1))
    submit(limit(3, SELL, 101, 5, account=2))

    events = submit(limit(4, BUY, 101, 6, account=1, stp=STP.CANCEL_TAKER))

    assert [type(e) for e in events] == [OrderAccepted, Trade, OrderCancelled]
    assert events[-1] == OrderCancelled(4, 4000, 4, 1, 4, CancelReason.SELF_TRADE)
    assert [e.order_id for e in engine.book.entries()] == [2, 3]


def test_stp_cancel_maker_removes_own_orders_and_keeps_matching(
    submit: Submit, engine: MatchingEngine
) -> None:
    submit(limit(1, SELL, 100, 5, account=1))
    submit(limit(2, SELL, 100, 3, account=2))

    events = submit(limit(3, BUY, 100, 3, account=1, stp=STP.CANCEL_MAKER))

    assert events[1] == OrderCancelled(3, 3000, 1, 1, 5, CancelReason.SELF_TRADE)
    assert isinstance(events[2], Trade)
    assert events[2].maker_order_id == 2
    assert len(engine.book) == 0


def test_stp_cancel_both_removes_maker_and_taker(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, SELL, 100, 5, account=1))

    events = submit(limit(2, BUY, 100, 3, account=1, stp=STP.CANCEL_BOTH))

    assert events[1:] == [
        OrderCancelled(2, 2000, 1, 1, 5, CancelReason.SELF_TRADE),
        OrderCancelled(2, 2000, 2, 1, 3, CancelReason.SELF_TRADE),
    ]
    assert len(engine.book) == 0


def test_stp_none_lets_an_account_trade_with_itself(submit: Submit) -> None:
    submit(limit(1, SELL, 100, 5, account=1))

    events = submit(market(2, BUY, 5, account=1, stp=STP.NONE))

    assert isinstance(events[1], Trade)


def test_fok_counts_liquidity_the_way_stp_would_match_it(submit: Submit) -> None:
    submit(limit(1, SELL, 100, 5, account=1))
    submit(limit(2, SELL, 100, 5, account=2))

    # Cancel-taker would stop at the own order, so none of the 5 is reachable.
    blocked = submit(limit(3, BUY, 100, 5, account=1, tif=TimeInForce.FOK))
    # Cancel-maker skips the own order, so order 2 alone fills it.
    filled = submit(limit(4, BUY, 100, 5, account=1, tif=TimeInForce.FOK, stp=STP.CANCEL_MAKER))

    assert blocked == [OrderRejected(3, 3000, 3, 1, RejectReason.FOK_NOT_FILLABLE)]
    assert [type(e) for e in filled] == [OrderAccepted, OrderCancelled, Trade]
