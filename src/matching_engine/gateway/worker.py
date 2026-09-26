"""One asyncio task per market: the only code that touches that market's engine.

Per batch, the worker

1. drains up to ``batch_max`` queued requests without waiting,
2. stamps each one through the sequencer (order ids are the sequence number),
3. appends them all to the WAL and makes them durable with **one** fsync,
4. only then applies them to the engine, resolves each caller's future and
   hands the batch's events to the market-data hub.

Nothing is acknowledged before it is on disk, and while one fsync is in
flight the next batch fills up behind it — group commit falls out of the loop
shape rather than needing a timer.

The engine is mutated only between awaits, on the event-loop thread, so
request handlers may read it (depth, L3) without locks: they can never observe
a half-applied command.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from matching_engine.domain import Command, SequencedCommand
from matching_engine.engine import MatchingEngine
from matching_engine.events import Event
from matching_engine.gateway.marketdata import MarketDataHub
from matching_engine.journal import Journal, RecoveryReport
from matching_engine.market import MarketSpec
from matching_engine.sequencer import Sequencer
from matching_engine.snapshot import encode_snapshot

log = logging.getLogger(__name__)


class MarketUnavailableError(Exception):
    """The market cannot take the request: overloaded, stopping, or failed."""


@dataclass(slots=True)
class _Request:
    build: Callable[[int], Command]  # receives the seq, so a new order can use it as its id
    future: asyncio.Future[tuple[SequencedCommand, list[Event]]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


_STOP = object()


class MarketWorker:
    def __init__(
        self,
        spec: MarketSpec,
        journal: Journal,
        engine: MatchingEngine,
        report: RecoveryReport,
        hub: MarketDataHub,
        *,
        batch_max: int,
        queue_max: int,
        snapshot_every: int,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self.spec = spec
        self.engine = engine
        self.hub = hub
        self._journal = journal
        self._sequencer = Sequencer(report.last_seq + 1, report.last_ts, clock)
        self._queue: asyncio.Queue[_Request | object] = asyncio.Queue(maxsize=queue_max)
        self._batch_max = batch_max
        self._snapshot_every = snapshot_every
        self._since_snapshot = report.replayed
        self._task: asyncio.Task[None] | None = None
        self._accepting = False
        self.failure: BaseException | None = None

    def start(self) -> None:
        self._journal.open(self._sequencer.next_seq)
        self._accepting = True
        self._task = asyncio.create_task(self._run(), name=f"market:{self.spec.symbol}")

    async def stop(self) -> None:
        """Finish everything already queued, checkpoint, and close the log."""
        self._accepting = False
        if self._task is not None and not self._task.done():
            await self._queue.put(_STOP)
            await self._task
        if self.failure is None:  # a failed log is left exactly as it was for inspection
            if self._since_snapshot:
                await self._checkpoint()
            self._journal.close()

    async def submit(self, build: Callable[[int], Command]) -> tuple[SequencedCommand, list[Event]]:
        """Queue one command and wait until it is durable and applied."""
        if not self._accepting:
            raise MarketUnavailableError(f"{self.spec.symbol} is not accepting orders")
        request = _Request(build)
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            raise MarketUnavailableError(f"{self.spec.symbol} is overloaded") from None
        return await request.future

    async def _run(self) -> None:
        batch: list[_Request] = []
        try:
            while True:
                item = await self._queue.get()
                stop = item is _STOP
                if not stop:
                    assert isinstance(item, _Request)
                    batch.append(item)
                while not stop and len(batch) < self._batch_max:
                    try:
                        item = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if item is _STOP:
                        stop = True
                    else:
                        assert isinstance(item, _Request)
                        batch.append(item)
                if batch:
                    await self._commit(batch)
                    batch.clear()
                if self._since_snapshot >= self._snapshot_every:
                    await self._checkpoint()
                if stop:
                    return
        except Exception as exc:
            # Fail-stop: a market that cannot write its log must not keep matching.
            log.critical("market %s stopped: %r", self.spec.symbol, exc)
            self.failure = exc
            self._accepting = False
            for request in batch:
                if not request.future.done():
                    request.future.set_exception(MarketUnavailableError(str(exc)))
            self._fail_queued(exc)

    async def _commit(self, batch: list[_Request]) -> None:
        sequencer, journal = self._sequencer, self._journal
        stamped = [sequencer.stamp(r.build(sequencer.next_seq)) for r in batch]
        for sc in stamped:
            journal.append(sc)
        if journal.fsync:
            await asyncio.to_thread(journal.sync)
        else:
            journal.sync()
        process = self.engine.process
        published: list[Event] = []
        for request, sc in zip(batch, stamped, strict=True):
            events = process(sc)
            published += events
            if not request.future.done():  # the caller may have disconnected
                request.future.set_result((sc, events))
        self.hub.publish(published, self.engine)
        self._since_snapshot += len(batch)

    async def _checkpoint(self) -> None:
        blob = encode_snapshot(self.engine, self._sequencer.last_ts)
        await asyncio.to_thread(self._journal.checkpoint, blob, self.engine.last_seq)
        self._since_snapshot = 0

    def _fail_queued(self, exc: BaseException) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if isinstance(item, _Request) and not item.future.done():
                item.future.set_exception(MarketUnavailableError(str(exc)))
