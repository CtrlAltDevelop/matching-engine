"""Market specification: the one place decimals are turned into ticks and lots.

Clients speak decimal strings (``"64250.50"``); the engine speaks integers.
Conversion happens once at the edge, and it is strict: a price that is not a
whole number of ticks is refused rather than rounded, because silently moving
someone's limit price is worse than rejecting the order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from matching_engine.domain import MAX_INT64


@dataclass(frozen=True, slots=True)
class MarketSpec:
    symbol: str
    tick_size: Decimal  # price increment: price = ticks * tick_size
    lot_size: Decimal  # quantity increment: qty = lots * lot_size

    @classmethod
    def parse(cls, text: str) -> MarketSpec:
        """Parse ``SYMBOL:TICK:LOT``, e.g. ``BTC-USDT:0.01:0.00001``."""
        try:
            symbol, tick, lot = text.strip().split(":")
            spec = cls(symbol, Decimal(tick), Decimal(lot))
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"expected SYMBOL:TICK:LOT, got {text!r}") from exc
        if not symbol or spec.tick_size <= 0 or spec.lot_size <= 0:
            raise ValueError(f"invalid market spec {text!r}")
        return spec

    def to_ticks(self, price: Decimal) -> int:
        return _to_units(price, self.tick_size, "price")

    def to_lots(self, qty: Decimal) -> int:
        return _to_units(qty, self.lot_size, "quantity")

    def price(self, ticks: int) -> str:
        return f"{self.tick_size * ticks:f}"

    def qty(self, lots: int) -> str:
        return f"{self.lot_size * lots:f}"


def _to_units(value: Decimal, step: Decimal, what: str) -> int:
    units = value / step
    if units != units.to_integral_value():
        raise ValueError(f"{what} {value} is not a multiple of {step}")
    if not 0 < units <= MAX_INT64:
        raise ValueError(f"{what} {value} is out of range")
    return int(units)
