from matching_engine.book import BookEntry, DepthLevel, OrderBook, RestingOrder
from matching_engine.domain import Side


def _order(order_id: int, side: Side, price: int, qty: int, account: int = 1) -> RestingOrder:
    return RestingOrder(order_id, account, side, price, qty)


def test_best_prices_are_the_highest_bid_and_the_lowest_ask() -> None:
    book = OrderBook()
    for oid, price in enumerate((99, 101, 100), start=1):
        book.add(_order(oid, Side.BUY, price, 1))
    for oid, price in enumerate((105, 103, 104), start=10):
        book.add(_order(oid, Side.SELL, price, 1))

    assert book.best_bid() == 101
    assert book.best_ask() == 103


def test_empty_book_has_no_best_prices() -> None:
    book = OrderBook()

    assert book.best_bid() is None
    assert book.best_ask() is None
    assert book.depth() == book.depth(5)


def test_orders_at_one_price_queue_oldest_first() -> None:
    book = OrderBook()
    for oid in (3, 1, 2):
        book.add(_order(oid, Side.SELL, 100, oid))

    assert [e.order_id for e in book.entries()] == [3, 1, 2]


def test_removing_from_the_middle_keeps_the_queue_linked() -> None:
    book = OrderBook()
    orders = [_order(oid, Side.BUY, 100, 10) for oid in (1, 2, 3)]
    for o in orders:
        book.add(o)

    book.remove(orders[1])

    assert [e.order_id for e in book.entries()] == [1, 3]
    assert book.depth().bids == [DepthLevel(price=100, qty=20, orders=2)]


def test_removing_the_last_order_drops_the_level() -> None:
    book = OrderBook()
    only = _order(1, Side.SELL, 100, 5)
    book.add(only)
    book.add(_order(2, Side.SELL, 101, 5))

    book.remove(only)

    assert book.best_ask() == 101
    assert 100 * book.asks.sign not in book.asks.levels
    assert len(book) == 1


def test_depth_aggregates_levels_best_first_and_honours_the_limit() -> None:
    book = OrderBook()
    book.add(_order(1, Side.BUY, 100, 4))
    book.add(_order(2, Side.BUY, 100, 6))
    book.add(_order(3, Side.BUY, 98, 1))
    book.add(_order(4, Side.BUY, 99, 2))

    assert book.depth().bids == [
        DepthLevel(100, 10, 2),
        DepthLevel(99, 2, 1),
        DepthLevel(98, 1, 1),
    ]
    assert book.depth(2).bids == [DepthLevel(100, 10, 2), DepthLevel(99, 2, 1)]


def test_l3_lists_bids_then_asks_in_priority_order() -> None:
    book = OrderBook()
    book.add(_order(1, Side.SELL, 102, 1, account=7))
    book.add(_order(2, Side.BUY, 99, 2, account=8))
    book.add(_order(3, Side.SELL, 101, 3, account=9))

    assert list(book.entries()) == [
        BookEntry(2, 8, Side.BUY, 99, 2),
        BookEntry(3, 9, Side.SELL, 101, 3),
        BookEntry(1, 7, Side.SELL, 102, 1),
    ]
