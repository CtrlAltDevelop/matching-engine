"""REST gateway: order entry, cancels, book views, restarts and backpressure."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from matching_engine.domain import CancelOrder
from matching_engine.gateway import Settings, create_app
from matching_engine.gateway.marketdata import MarketDataHub
from matching_engine.gateway.worker import MarketUnavailableError, MarketWorker
from matching_engine.journal import Journal
from matching_engine.market import MarketSpec

BTC = MarketSpec("BTC-USDT", Decimal("0.01"), Decimal("0.001"))
ETH = MarketSpec("ETH-USDT", Decimal("0.05"), Decimal("0.01"))


def settings(root: Path, **overrides: Any) -> Settings:
    return Settings(data_dir=root, markets=(BTC, ETH), fsync=False, **overrides)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(settings(tmp_path))) as c:
        yield c


def order(client: TestClient, **body: Any) -> httpx2.Response:
    return client.post("/markets/BTC-USDT/orders", json={"account": 1, **body})


def test_a_resting_limit_order_is_acknowledged_with_decimal_strings(client: TestClient) -> None:
    resp = order(client, side="buy", price="64250.50", quantity="0.250")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resting"
    assert body["order_id"] == body["seq"] == 1
    assert body["events"] == [
        {
            "type": "order_accepted",
            "seq": 1,
            "ts": body["events"][0]["ts"],
            "order_id": 1,
            "account": 1,
            "side": "buy",
            "order_type": "limit",
            "tif": "gtc",
            "price": "64250.50",
            "qty": "0.250",
        }
    ]


def test_a_crossing_order_reports_its_fills(client: TestClient) -> None:
    order(client, account=2, side="sell", price="100.00", quantity="1.000")

    body = order(client, side="buy", price="101", quantity="0.4").json()

    assert body["status"] == "filled"
    assert body["filled_quantity"] == "0.400"
    trade = body["events"][1]
    assert trade["type"] == "trade"
    assert (trade["price"], trade["qty"], trade["maker_remaining"]) == ("100.00", "0.400", "0.600")


def test_a_market_order_defaults_to_ioc_and_reports_the_unfilled_part(client: TestClient) -> None:
    order(client, account=2, side="sell", price="100", quantity="1")

    body = order(client, side="buy", type="market", quantity="3").json()

    assert body["status"] == "cancelled"
    assert body["filled_quantity"] == "1.000"
    assert body["events"][-1]["remaining"] == "2.000"


def test_business_rejects_are_a_normal_response(client: TestClient) -> None:
    body = order(client, side="buy", price="100", quantity="1", time_in_force="fok").json()

    assert body["status"] == "rejected"
    assert body["events"][0]["reason"] == "fok_not_fillable"


@pytest.mark.parametrize(
    "body",
    [
        {"side": "buy", "price": "100.001", "quantity": "1"},  # off the tick grid
        {"side": "buy", "price": "100", "quantity": "0.0001"},  # off the lot grid
        {"side": "buy", "quantity": "1"},  # limit without a price
        {"side": "buy", "type": "market", "price": "1", "quantity": "1"},
        {"side": "buy", "price": "-1", "quantity": "1"},
        {"side": "buy", "price": "100", "quantity": "1", "leverage": 100},
        {"side": "sideways", "price": "100", "quantity": "1"},
    ],
)
def test_malformed_orders_never_reach_the_engine(client: TestClient, body: dict[str, Any]) -> None:
    resp = order(client, **body)

    assert resp.status_code == 422
    assert client.get("/markets").json()[0]["last_seq"] == 0


def test_unknown_markets_are_404(client: TestClient) -> None:
    assert client.get("/markets/DOGE-USDT/depth").status_code == 404


def test_cancel_is_owner_only(client: TestClient) -> None:
    order(client, account=5, side="sell", price="100", quantity="1")

    wrong = client.delete("/markets/BTC-USDT/orders/1", params={"account": 6}).json()
    right = client.delete("/markets/BTC-USDT/orders/1", params={"account": 5}).json()

    assert wrong["status"] == "rejected"
    assert wrong["events"][0]["reason"] == "not_order_owner"
    assert right["status"] == "cancelled"
    assert right["events"][0]["remaining"] == "1.000"


def test_depth_and_l3_views(client: TestClient) -> None:
    order(client, account=2, side="buy", price="99.50", quantity="1")
    order(client, account=3, side="buy", price="99.50", quantity="2")
    order(client, account=4, side="sell", price="100.25", quantity="0.5")

    depth = client.get("/markets/BTC-USDT/depth", params={"levels": 5}).json()
    book = client.get("/markets/BTC-USDT/book").json()

    assert depth["bids"] == [["99.50", "3.000", 2]]
    assert depth["asks"] == [["100.25", "0.500", 1]]
    assert depth["seq"] == 3
    assert [(o["order_id"], o["side"]) for o in book["orders"]] == [
        (1, "buy"),
        (2, "buy"),
        (3, "sell"),
    ]
    assert "account" not in book["orders"][0]


def test_markets_are_independent(client: TestClient) -> None:
    order(client, side="buy", price="100", quantity="1")
    eth = client.post(
        "/markets/ETH-USDT/orders",
        json={"account": 1, "side": "sell", "price": "3000.05", "quantity": "2.5"},
    ).json()

    markets = {m["symbol"]: m for m in client.get("/markets").json()}
    assert eth["seq"] == 1, "each market has its own sequence"
    assert markets["BTC-USDT"]["best_bid"] == "100.00"
    assert markets["ETH-USDT"]["best_ask"] == "3000.05"
    assert markets["ETH-USDT"]["best_bid"] is None


def test_a_restarted_gateway_resumes_the_same_book_and_sequence(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path))) as first:
        order(first, account=2, side="sell", price="100", quantity="2")
        order(first, side="buy", price="100", quantity="0.5")
        before = first.get("/markets/BTC-USDT/depth").json()

    with TestClient(create_app(settings(tmp_path))) as second:
        after = second.get("/markets/BTC-USDT/depth").json()
        next_order = order(second, side="buy", price="90", quantity="1").json()

    assert after == before
    assert next_order["seq"] == 3
    assert any((tmp_path / "BTC-USDT" / "snapshots").glob("*.snap")), "shutdown checkpoints"


def test_concurrent_orders_get_unique_consecutive_sequence_numbers(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path, batch_max=16))

    async def scenario() -> list[int]:
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
                replies = await asyncio.gather(
                    *(
                        http.post(
                            "/markets/BTC-USDT/orders",
                            json={"account": i, "side": "buy", "price": "10", "quantity": "1"},
                        )
                        for i in range(1, 101)
                    )
                )
        return sorted(r.json()["seq"] for r in replies)

    assert asyncio.run(scenario()) == list(range(1, 101))


def test_a_full_queue_refuses_new_work_instead_of_growing(tmp_path: Path) -> None:
    async def scenario() -> list[object]:
        journal = Journal(tmp_path, fsync=False)
        engine, report = journal.recover()
        hub = MarketDataHub(BTC, depth_levels=5, subscriber_queue=10)
        worker = MarketWorker(
            BTC, journal, engine, report, hub, batch_max=8, queue_max=2, snapshot_every=1_000
        )
        worker.start()
        submits = [
            asyncio.create_task(worker.submit(lambda _seq: CancelOrder(1, 1))) for _ in range(5)
        ]
        results: list[object] = await asyncio.gather(*submits, return_exceptions=True)
        await worker.stop()
        return results

    results = asyncio.run(scenario())

    refused = [r for r in results if isinstance(r, MarketUnavailableError)]
    assert len(refused) == 3
    assert "overloaded" in str(refused[0])


def test_health(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
