"""FastAPI application: REST order entry, book views and a WebSocket feed per market."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import msgspec
from fastapi import APIRouter, FastAPI, HTTPException, Path, Query, Request, WebSocket, status

from matching_engine import __version__
from matching_engine.domain import MAX_INT64, CancelOrder, Side
from matching_engine.events import OrderCancelled
from matching_engine.gateway.config import Settings
from matching_engine.gateway.marketdata import MarketDataHub, Subscriber
from matching_engine.gateway.schemas import (
    BookOrder,
    BookResponse,
    CancelResponse,
    DepthResponse,
    MarketInfo,
    OrderRequest,
    OrderResponse,
    order_outcome,
    render_depth,
    render_event,
)
from matching_engine.gateway.worker import MarketUnavailableError, MarketWorker
from matching_engine.journal import Journal

Symbol = Annotated[str, Path(description="Market symbol, e.g. BTC-USDT")]

router = APIRouter()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        workers = open_markets(settings)
        for worker in workers.values():
            worker.start()
        app.state.workers = workers
        try:
            yield
        finally:
            for worker in workers.values():
                await worker.stop()

    app = FastAPI(
        title="Matching Engine Gateway",
        version=__version__,
        summary="Price-time priority order entry and market data.",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


def open_markets(settings: Settings) -> dict[str, MarketWorker]:
    """Recover every configured market from its own directory."""
    workers = {}
    for spec in settings.markets:
        journal = Journal(settings.data_dir / spec.symbol, fsync=settings.fsync)
        engine, report = journal.recover()
        hub = MarketDataHub(
            spec, depth_levels=settings.depth_levels, subscriber_queue=settings.subscriber_queue
        )
        workers[spec.symbol] = MarketWorker(
            spec,
            journal,
            engine,
            report,
            hub,
            batch_max=settings.batch_max,
            queue_max=settings.queue_max,
            snapshot_every=settings.snapshot_every,
        )
    return workers


def _market(request: Request, symbol: str) -> MarketWorker:
    worker: MarketWorker | None = request.app.state.workers.get(symbol)
    if worker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown market {symbol}")
    return worker


@router.get("/healthz")
async def health(request: Request) -> dict[str, str]:
    failed = [s for s, w in request.app.state.workers.items() if w.failure is not None]
    if failed:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"failed: {failed}")
    return {"status": "ok"}


@router.get("/markets")
async def list_markets(request: Request) -> list[MarketInfo]:
    infos = []
    for worker in request.app.state.workers.values():
        spec, book = worker.spec, worker.engine.book
        bid, ask = book.best_bid(), book.best_ask()
        infos.append(
            MarketInfo(
                symbol=spec.symbol,
                tick_size=str(spec.tick_size),
                lot_size=str(spec.lot_size),
                last_seq=worker.engine.last_seq,
                best_bid=None if bid is None else spec.price(bid),
                best_ask=None if ask is None else spec.price(ask),
            )
        )
    return infos


@router.post("/markets/{symbol}/orders")
async def place_order(request: Request, symbol: Symbol, body: OrderRequest) -> OrderResponse:
    worker = _market(request, symbol)
    try:
        template = body.to_command(worker.spec, order_id=0)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    try:
        # The order id is the sequence number, assigned inside the worker.
        sc, events = await worker.submit(
            lambda seq: msgspec.structs.replace(template, order_id=seq)
        )
    except MarketUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    order = msgspec.structs.replace(template, order_id=sc.seq)
    outcome, filled = order_outcome(order, events)
    return OrderResponse(
        order_id=order.order_id,
        seq=sc.seq,
        status=outcome,
        filled_quantity=worker.spec.qty(filled),
        events=[render_event(worker.spec, e) for e in events],
    )


@router.delete("/markets/{symbol}/orders/{order_id}")
async def cancel_order(
    request: Request,
    symbol: Symbol,
    order_id: Annotated[int, Path(ge=1, le=MAX_INT64)],
    account: Annotated[int, Query(ge=1, le=MAX_INT64)],
) -> CancelResponse:
    worker = _market(request, symbol)
    try:
        sc, events = await worker.submit(lambda _seq: CancelOrder(order_id, account))
    except MarketUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    cancelled = any(isinstance(e, OrderCancelled) for e in events)
    return CancelResponse(
        order_id=order_id,
        seq=sc.seq,
        status="cancelled" if cancelled else "rejected",
        events=[render_event(worker.spec, e) for e in events],
    )


@router.get("/markets/{symbol}/depth")
async def depth(
    request: Request, symbol: Symbol, levels: Annotated[int, Query(ge=1, le=1_000)] = 20
) -> DepthResponse:
    worker = _market(request, symbol)
    return render_depth(worker.spec, worker.engine.last_seq, worker.engine.book.depth(levels))


@router.get("/markets/{symbol}/book")
async def book(
    request: Request, symbol: Symbol, limit: Annotated[int, Query(ge=1, le=10_000)] = 1_000
) -> BookResponse:
    """L3: resting orders in priority order, up to ``limit`` per side. Accounts stay private."""
    worker = _market(request, symbol)
    spec, entries = worker.spec, worker.engine.book.entries
    orders = [
        BookOrder(
            order_id=e.order_id, side=e.side, price=spec.price(e.price), quantity=spec.qty(e.qty)
        )
        for side in Side
        for e in itertools.islice(entries(side), limit)
    ]
    return BookResponse(symbol=spec.symbol, seq=worker.engine.last_seq, orders=orders)


@router.websocket("/markets/{symbol}/ws")
async def feed(ws: WebSocket, symbol: str) -> None:
    """Depth snapshot on connect, then trades and depth changes as they happen."""
    worker: MarketWorker | None = ws.app.state.workers.get(symbol)
    if worker is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason=f"unknown market {symbol}")
        return
    await ws.accept()
    subscriber, snapshot = worker.hub.subscribe(worker.engine)
    try:
        await ws.send_text(snapshot)
        pump = asyncio.create_task(_pump(ws, subscriber))
        hangup = asyncio.create_task(_wait_for_disconnect(ws))
        _, pending = await asyncio.wait({pump, hangup}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    finally:
        worker.hub.unsubscribe(subscriber)


async def _pump(ws: WebSocket, subscriber: Subscriber) -> None:
    while (message := await subscriber.queue.get()) is not None:
        await ws.send_text(message)
    # The hub dropped us for falling behind: say so, and let the client resync.
    await ws.close(code=status.WS_1013_TRY_AGAIN_LATER, reason="subscriber too slow")


async def _wait_for_disconnect(ws: WebSocket) -> None:
    """The feed is one-way; anything the client sends is ignored until it leaves."""
    while (await ws.receive())["type"] != "websocket.disconnect":
        pass
