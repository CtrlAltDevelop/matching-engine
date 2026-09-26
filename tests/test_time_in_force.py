"""GTC rests, IOC cancels its remainder, FOK fills whole or not at all."""

import msgspec
import pytest
from helpers import Submit, limit, market

from matching_engine.domain import Side, TimeInForce
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
IOC, FOK = TimeInForce.IOC, TimeInForce.FOK


def test_ioc_fills_what_crosses_and_cancels_the_rest(
    submit: Submit, engine: MatchingEngine
) -> None:
    submit(limit(1, SELL, 100, 3, account=2))

    events = submit(limit(2, BUY, 100, 5, tif=IOC))

    assert [type(e) for e in events] == [OrderAccepted, Trade, OrderCancelled]
    assert events[-1] == OrderCancelled(2, 2000, 2, 1, 2, CancelReason.UNFILLED)
    assert engine.book.best_bid() is None


def test_ioc_that_crosses_nothing_is_cancelled_whole(submit: Submit) -> None:
    submit(limit(1, SELL, 101, 3, account=2))

    events = submit(limit(2, BUY, 100, 5, tif=IOC))

    assert events[-1] == OrderCancelled(2, 2000, 2, 1, 5, CancelReason.UNFILLED)


def test_fok_is_rejected_when_the_book_is_too_thin(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, SELL, 100, 3, account=2))
    submit(limit(2, SELL, 101, 3, account=2))
    before = engine.book.depth()

    events = submit(limit(3, BUY, 101, 7, tif=FOK))

    assert events == [OrderRejected(3, 3000, 3, 1, RejectReason.FOK_NOT_FILLABLE)]
    assert engine.book.depth() == before


def test_fok_ignores_liquidity_beyond_its_limit(submit: Submit) -> None:
    submit(limit(1, SELL, 100, 3, account=2))
    submit(limit(2, SELL, 105, 10, account=2))

    events = submit(limit(3, BUY, 104, 4, tif=FOK))

    assert events == [OrderRejected(3, 3000, 3, 1, RejectReason.FOK_NOT_FILLABLE)]


def test_fok_fills_completely_across_levels(submit: Submit, engine: MatchingEngine) -> None:
    submit(limit(1, SELL, 100, 3, account=2))
    submit(limit(2, SELL, 101, 3, account=2))

    events = submit(limit(3, BUY, 101, 6, tif=FOK))

    assert sum(e.qty for e in events if isinstance(e, Trade)) == 6
    assert len(engine.book) == 0


def test_market_fok_needs_the_whole_quantity_available(submit: Submit) -> None:
    submit(limit(1, BUY, 100, 2, account=2))

    assert submit(market(2, SELL, 3, tif=FOK))[0] == OrderRejected(
        2, 2000, 2, 1, RejectReason.FOK_NOT_FILLABLE
    )
    assert isinstance(submit(market(3, SELL, 2, tif=FOK))[1], Trade)


def test_a_market_order_cannot_be_good_till_cancelled(submit: Submit) -> None:
    order = msgspec.structs.replace(market(1, BUY, 1), tif=TimeInForce.GTC)

    assert submit(order) == [OrderRejected(1, 1000, 1, 1, RejectReason.INVALID_TIME_IN_FORCE)]


@pytest.mark.parametrize("tif", [IOC, FOK])
def test_immediate_orders_never_rest(
    submit: Submit, engine: MatchingEngine, tif: TimeInForce
) -> None:
    submit(limit(1, BUY, 100, 5, tif=tif))

    assert len(engine.book) == 0
