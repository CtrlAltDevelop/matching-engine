"""WebSocket market data: snapshot on connect, live trades and depth, slow consumers."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import limit
from starlette.websockets import WebSocketDisconnect

from matching_engine.domain import SequencedCommand, Side
from matching_engine.engine import MatchingEngine
from matching_engine.gateway import Settings, create_app
from matching_engine.gateway.marketdata import MarketDataHub
from matching_engine.market import MarketSpec

BTC = MarketSpec("BTC-USDT", Decimal("0.01"), Decimal("0.001"))


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Settings(data_dir=tmp_path, markets=(BTC,), fsync=False)))


def test_a_subscriber_gets_depth_then_live_trades(client: TestClient) -> None:
    with client:
        client.post(
            "/markets/BTC-USDT/orders",
            json={"account": 2, "side": "sell", "price": "100", "quantity": "1"},
        )
        with client.websocket_connect("/markets/BTC-USDT/ws") as ws:
            snapshot = ws.receive_json()
            client.post(
                "/markets/BTC-USDT/orders",
                json={"account": 1, "side": "buy", "price": "100", "quantity": "0.4"},
            )
            trade = ws.receive_json()
            depth = ws.receive_json()

    assert snapshot == {
        "type": "depth",
        "symbol": "BTC-USDT",
        "seq": 1,
        "bids": [],
        "asks": [["100.00", "1.000", 1]],
    }
    assert trade["type"] == "trade"
    assert (trade["price"], trade["qty"], trade["taker_side"], trade["seq"]) == (
        "100.00",
        "0.400",
        "buy",
        2,
    )
    assert depth["asks"] == [["100.00", "0.600", 1]]
    assert depth["seq"] == 2


def test_depth_is_not_repeated_when_a_batch_leaves_it_unchanged(client: TestClient) -> None:
    with client, client.websocket_connect("/markets/BTC-USDT/ws") as ws:
        ws.receive_json()
        client.delete("/markets/BTC-USDT/orders/99", params={"account": 1})  # rejected: no change
        client.post(
            "/markets/BTC-USDT/orders",
            json={"account": 1, "side": "buy", "price": "99", "quantity": "1"},
        )
        message = ws.receive_json()

    assert message["seq"] == 2, "the rejected cancel (seq 1) must not produce a depth push"


def test_an_unknown_market_feed_is_refused(client: TestClient) -> None:
    with (
        client,
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/markets/NOPE/ws") as ws,
    ):
        ws.receive_json()

    assert refused.value.code == 1008


def test_a_slow_subscriber_is_dropped_instead_of_stalling_the_market() -> None:
    async def scenario() -> tuple[list[str | None], int]:
        engine = MatchingEngine()
        hub = MarketDataHub(BTC, depth_levels=5, subscriber_queue=3)
        subscriber, _ = hub.subscribe(engine)
        for seq in range(1, 7):  # every order moves the depth: one message each
            events = engine.process(SequencedCommand(seq, seq, limit(seq, Side.BUY, seq, 1)))
            hub.publish(events, engine)
        drained = []
        while not subscriber.queue.empty():
            drained.append(subscriber.queue.get_nowait())
        return drained, hub.dropped

    drained, dropped = asyncio.run(scenario())

    assert dropped == 1
    assert len(drained) == 4
    assert drained[-1] is None, "the hang-up sentinel follows the backlog"
    assert [json.loads(m)["seq"] for m in drained if m is not None] == [1, 2, 3]
