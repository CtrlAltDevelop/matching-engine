"""Stop orders: parked off-book until the last trade price reaches the stop."""

import msgspec
from helpers import Submit, cancel, limit, market

from matching_engine.domain import NewOrder, Side
from matching_engine.engine import MatchingEngine
from matching_engine.events import (
    OrderAccepted,
    OrderCancelled,
    OrderRejected,
    OrderTriggered,
    RejectReason,
    Trade,
)

BUY, SELL = Side.BUY, Side.SELL


def stop(
    order_id: int, side: Side, stop_price: int, price: int, qty: int, account: int = 1
) -> NewOrder:
    return msgspec.structs.replace(
        limit(order_id, side, price, qty, account=account), stop_price=stop_price
    )


def stop_market(order_id: int, side: Side, stop_price: int, qty: int, account: int = 1) -> NewOrder:
    return msgspec.structs.replace(
        market(order_id, side, qty, account=account), stop_price=stop_price
    )


def trade_at(submit: Submit, price: int, first_id: int = 100) -> None:
    """Print a trade at ``price`` between two throwaway accounts."""
    submit(limit(first_id, SELL, price, 1, account=8))
    submit(limit(first_id + 1, BUY, price, 1, account=9))


def test_a_stop_is_parked_off_book(submit: Submit, engine: MatchingEngine) -> None:
    events = submit(stop(1, BUY, 105, 106, 2))

    assert [type(e) for e in events] == [OrderAccepted]
    assert events[0].stop_price == 105  # type: ignore[union-attr]
    assert len(engine.book) == 0
    assert engine.book.best_bid() is None


def test_a_buy_stop_triggers_when_the_last_trade_reaches_it(
    submit: Submit, engine: MatchingEngine
) -> None:
    submit(stop(1, BUY, 105, 106, 2))
    submit(limit(2, SELL, 106, 5, account=2))
    trade_at(submit, 104)
    assert len(engine.book) == 1  # not yet: the stop is still parked

    submit(limit(3, SELL, 105, 1, account=8))
    events = submit(limit(4, BUY, 105, 1, account=9))

    kinds = [type(e) for e in events]
    assert kinds == [OrderAccepted, Trade, OrderTriggered, Trade]
    assert events[3].price == 106  # type: ignore[union-attr]
    assert [e.qty for e in engine.book.entries(SELL)] == [3]


def test_a_sell_stop_triggers_downward(submit: Submit, engine: MatchingEngine) -> None:
    submit(stop_market(1, SELL, 95, 2))
    submit(limit(2, BUY, 94, 5, account=2))

    trade_at(submit, 96)
    assert [e.qty for e in engine.book.entries(BUY)] == [5]

    trade_at(submit, 95, first_id=200)
    assert [e.qty for e in engine.book.entries(BUY)] == [3]


def test_a_stop_already_through_the_market_triggers_at_once(submit: Submit) -> None:
    trade_at(submit, 100)

    events = submit(stop_market(1, BUY, 99, 1))

    assert [type(e) for e in events] == [OrderAccepted, OrderTriggered, OrderCancelled]


def test_triggered_limit_remainder_rests(submit: Submit, engine: MatchingEngine) -> None:
    submit(stop(1, BUY, 100, 101, 4))
    trade_at(submit, 100)

    assert [(e.order_id, e.price, e.qty) for e in engine.book.entries(BUY)] == [(1, 101, 4)]


def test_stops_trigger_in_arrival_order_and_cascade(submit: Submit, engine: MatchingEngine) -> None:
    submit(stop_market(1, BUY, 100, 1, account=3))
    submit(stop_market(2, BUY, 101, 1, account=4))
    submit(limit(3, SELL, 101, 1, account=5))
    submit(limit(4, SELL, 102, 1, account=5))

    events = submit(limit(5, BUY, 100, 1, account=7))  # nothing at 100: parks
    assert events == [events[0]]
    trade_at(submit, 100)

    # stop 1 buys at 101 -> last trade 101 -> stop 2 fires in the same command and buys at 102
    assert len(engine.book) == 0 or all(e.order_id not in (3, 4) for e in engine.book.entries())


def test_cancelling_a_parked_stop(submit: Submit) -> None:
    submit(stop(1, BUY, 105, 106, 2))

    ok = submit(cancel(1))
    again = submit(cancel(1))

    assert [type(e) for e in ok] == [OrderCancelled]
    assert isinstance(again[0], OrderRejected)


def test_duplicate_id_and_bad_stop_price_are_rejected(submit: Submit) -> None:
    submit(stop(1, BUY, 105, 106, 2))

    dup = submit(limit(1, BUY, 100, 1))
    bad = submit(stop(2, BUY, -1, 106, 1))

    assert dup[0].reason is RejectReason.DUPLICATE_ORDER_ID  # type: ignore[union-attr]
    assert bad[0].reason is RejectReason.INVALID_PRICE  # type: ignore[union-attr]


def test_stops_survive_export_and_restore(submit: Submit, engine: MatchingEngine) -> None:
    submit(stop(1, BUY, 105, 106, 2))
    trade_at(submit, 100)

    restored = MatchingEngine.restore(engine.export_state())

    assert restored.state_hash() == engine.state_hash()
    assert restored.stops.keys() == engine.stops.keys()
