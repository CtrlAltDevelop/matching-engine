from decimal import Decimal
from pathlib import Path

import pytest

from matching_engine.gateway.config import Settings
from matching_engine.market import MarketSpec

SPEC = MarketSpec.parse("BTC-USDT:0.01:0.00001")


def test_decimal_prices_convert_to_whole_ticks_and_back() -> None:
    assert SPEC.to_ticks(Decimal("64250.37")) == 6_425_037
    assert SPEC.price(6_425_037) == "64250.37"
    assert SPEC.to_lots(Decimal("0.5")) == 50_000
    assert SPEC.qty(50_000) == "0.50000"


@pytest.mark.parametrize("price", ["64250.375", "0", "-0.01", "1e30"])
def test_prices_off_the_grid_or_out_of_range_are_refused(price: str) -> None:
    with pytest.raises(ValueError, match="price"):
        SPEC.to_ticks(Decimal(price))


@pytest.mark.parametrize("text", ["BTC-USDT", "BTC:0.01", ":0.01:1", "X:0:1", "X:abc:1"])
def test_bad_market_specs_are_refused(text: str) -> None:
    with pytest.raises(ValueError, match=r"spec|expected"):
        MarketSpec.parse(text)


def test_settings_come_from_the_environment() -> None:
    s = Settings.from_env(
        {"ME_DATA_DIR": "/var/me", "ME_MARKETS": "A-B:0.5:1, C-D:1:0.1", "ME_FSYNC": "off"}
    )

    assert s.data_dir == Path("/var/me")
    assert [m.symbol for m in s.markets] == ["A-B", "C-D"]
    assert s.fsync is False
    assert s.batch_max == Settings().batch_max
