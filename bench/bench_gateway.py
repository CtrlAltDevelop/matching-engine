"""End-to-end order entry through a real uvicorn server over loopback HTTP.

    uv run python bench/bench_gateway.py

Starts ``matching-engine serve`` in a subprocess with a throwaway data
directory, then measures

* round-trip latency for one client sending orders one after another, and
* throughput with many concurrent clients spread over several processes,
  where group commit lets one fsync cover every order that arrived while the
  previous fsync was running.

Each is run with fsync on and off, so the cost of durability is visible.
"""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing
import os
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

import httpx2
from _common import SEED, machine, percentile

from matching_engine.domain import NewOrder, OrderType
from matching_engine.workload import order_flow


def order_bodies(count: int) -> list[dict[str, Any]]:
    """The benchmark workload's new orders as JSON bodies (cancels are skipped)."""
    bodies: list[dict[str, Any]] = []
    for c in order_flow(SEED, count * 2):
        if not isinstance(c, NewOrder):
            continue
        body: dict[str, Any] = {
            "account": c.account,
            "side": c.side.value,
            "quantity": f"{c.qty * 0.001:.3f}",
        }
        if c.order_type is OrderType.MARKET:
            body["type"] = "market"
        else:
            body["price"] = f"{c.price / 100:.2f}"
        bodies.append(body)
        if len(bodies) == count:
            break
    return bodies


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def start_server(data_dir: str, fsync: bool) -> tuple[subprocess.Popen[bytes], str]:
    port = free_port()
    env = {
        **os.environ,
        "ME_DATA_DIR": data_dir,
        "ME_MARKETS": "BTC-USDT:0.01:0.001",
        "ME_FSYNC": "1" if fsync else "0",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "matching_engine", "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx2.get(f"{base}/healthz").status_code == 200:
                return server, base
        except httpx2.TransportError:
            time.sleep(0.1)
    server.kill()
    raise RuntimeError("server did not start")


def sequential(base: str, bodies: list[dict[str, Any]]) -> list[int]:
    url = f"{base}/markets/BTC-USDT/orders"
    samples = []
    with httpx2.Client() as client:
        for body in bodies:
            t0 = time.perf_counter_ns()
            client.post(url, json=body).raise_for_status()
            samples.append(time.perf_counter_ns() - t0)
    return sorted(samples)


async def _client_share(url: str, bodies: list[dict[str, Any]], clients: int) -> None:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for body in bodies:
        queue.put_nowait(body)

    async def client_loop(http: httpx2.AsyncClient) -> None:
        while not queue.empty():
            (await http.post(url, json=queue.get_nowait())).raise_for_status()

    limits = httpx2.Limits(max_connections=clients, max_keepalive_connections=clients)
    async with httpx2.AsyncClient(limits=limits, timeout=60) as http:
        await asyncio.gather(*(client_loop(http) for _ in range(clients)))


def _load_process(job: tuple[str, list[dict[str, Any]], int, float]) -> float:
    """One load-generator process: wait for the common start time, send, report the end."""
    url, bodies, clients, start_at = job
    time.sleep(max(0.0, start_at - time.time()))
    asyncio.run(_client_share(url, bodies, clients))
    return time.time()


def concurrent(base: str, bodies: list[dict[str, Any]], clients: int, procs: int) -> float:
    """Orders/s with ``clients`` connections spread over ``procs`` client processes.

    A single Python process running an HTTP client tops out long before the
    server does, so the load is generated from several.
    """
    url = f"{base}/markets/BTC-USDT/orders"
    start_at = time.time() + 3.0  # every process is imported and waiting by then
    jobs = [(url, bodies[i::procs], clients // procs, start_at) for i in range(procs)]
    with multiprocessing.Pool(procs) as pool:
        finished = pool.map(_load_process, jobs)
    return len(bodies) / (max(finished) - start_at)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sequential", type=int, default=2_000)
    parser.add_argument("--concurrent", type=int, default=20_000)
    parser.add_argument("--clients", type=int, default=64)
    parser.add_argument("--procs", type=int, default=4, help="load-generator processes")
    args = parser.parse_args()

    print(f"machine: {machine()}")
    bodies = order_bodies(args.sequential + args.concurrent)
    for fsync in (True, False):
        # Windows may hold the killed server's file handles for a moment longer.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_dir:
            server, base = start_server(data_dir, fsync)
            try:
                samples = sequential(base, bodies[: args.sequential])
                rate = concurrent(base, bodies[args.sequential :], args.clients, args.procs)
            finally:
                server.terminate()
                server.wait()
        print(
            f"fsync={'on ' if fsync else 'off'} | 1 client: p50 {percentile(samples, 50):.0f} us, "
            f"p99 {percentile(samples, 99):.0f} us, p99.9 {percentile(samples, 99.9):.0f} us | "
            f"{args.clients} clients / {args.procs} procs: {rate:,.0f} orders/s"
        )


if __name__ == "__main__":
    main()
