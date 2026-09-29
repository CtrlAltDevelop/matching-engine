"""Command-line entry point: run the gateway, or verify a market's files offline."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import msgspec

from matching_engine.journal import Journal
from matching_engine.snapshot import SnapshotError
from matching_engine.wal import WalCorruptionError


def serve(host: str, port: int, *, access_log: bool) -> None:
    import uvicorn  # noqa: PLC0415 - the gateway extra is optional for engine-only installs

    from matching_engine.gateway import create_app  # noqa: PLC0415

    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_level="info",
        access_log=access_log,
        http="httptools",
    )


def verify(market_dir: Path) -> int:
    """Recover a market read-only and print what was found. Exit 1 on corruption."""
    try:
        engine, report = Journal(market_dir, fsync=False).recover(repair=False)
    except (WalCorruptionError, SnapshotError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    summary = {
        "ok": True,
        "report": msgspec.to_builtins(report),
        "state_hash": engine.state_hash(),
        "resting_orders": len(engine.book),
        "best_bid": engine.book.best_bid(),
        "best_ask": engine.book.best_ask(),
    }
    print(json.dumps(summary, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="matching-engine")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_cmd = commands.add_parser("serve", help="run the HTTP/WebSocket gateway")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8000)
    serve_cmd.add_argument(
        "--access-log", action="store_true", help="log every request (costs throughput)"
    )
    verify_cmd = commands.add_parser("verify", help="check a market directory without repairing")
    verify_cmd.add_argument("market_dir", type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "serve":
        serve(args.host, args.port, access_log=args.access_log)
        return 0
    return verify(args.market_dir)


if __name__ == "__main__":
    sys.exit(main())
