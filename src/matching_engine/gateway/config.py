"""Gateway settings, read from ``ME_*`` environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from matching_engine.market import MarketSpec

DEFAULT_MARKETS = "BTC-USDT:0.01:0.00001,ETH-USDT:0.01:0.0001"


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = Path("data")
    markets: tuple[MarketSpec, ...] = tuple(MarketSpec.parse(m) for m in DEFAULT_MARKETS.split(","))
    fsync: bool = True
    batch_max: int = 512  # commands per group commit
    queue_max: int = 10_000  # pending commands per market before new ones get 503
    snapshot_every: int = 100_000  # commands between checkpoints
    depth_levels: int = 20  # L2 levels pushed to WebSocket subscribers
    subscriber_queue: int = 1_000  # messages buffered per subscriber before it is dropped

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        defaults = cls()
        markets = env.get("ME_MARKETS")
        return cls(
            data_dir=Path(env.get("ME_DATA_DIR", str(defaults.data_dir))),
            markets=(
                tuple(MarketSpec.parse(m) for m in markets.split(",") if m.strip())
                if markets
                else defaults.markets
            ),
            fsync=env.get("ME_FSYNC", "1").lower() not in {"0", "false", "no", "off"},
            batch_max=int(env.get("ME_BATCH_MAX", defaults.batch_max)),
            queue_max=int(env.get("ME_QUEUE_MAX", defaults.queue_max)),
            snapshot_every=int(env.get("ME_SNAPSHOT_EVERY", defaults.snapshot_every)),
            depth_levels=int(env.get("ME_DEPTH_LEVELS", defaults.depth_levels)),
            subscriber_queue=int(env.get("ME_SUBSCRIBER_QUEUE", defaults.subscriber_queue)),
        )
