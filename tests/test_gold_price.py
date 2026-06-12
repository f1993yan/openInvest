from __future__ import annotations

from unittest.mock import patch

from utils.gold_price import (
    GOLD_OZ_PER_GRAM,
    GoldPriceSnapshot,
    _get_db_fallback_snapshot,
    get_gold_snapshot,
)


def test_snapshot_dataclass_has_is_stale_field():
    snap = GoldPriceSnapshot(
        gold_usd_per_oz=4600.0,
        usdcny_rate=6.85,
        spot_cny_per_gram=1012.5,
        bank_cny_per_gram=1012.5,
        offset_pct=0.0,
    )
    assert snap.is_stale is False


def test_db_fallback_returns_stale_snapshot():
    with patch("db.market_store.MarketStore") as MockStore:
        instance = MockStore.return_value
        instance.get_latest_price.side_effect = lambda sym: {
            "GC=F": 4600.0,
            "USDCNY=X": 6.85,
        }.get(sym)

        result = _get_db_fallback_snapshot(offset_pct=0.0)

    assert result is not None
    assert result.is_stale is True
    assert result.gold_usd_per_oz == 4600.0
    assert abs(result.spot_cny_per_gram - (4600.0 / GOLD_OZ_PER_GRAM * 6.85)) < 1e-3


def test_db_fallback_returns_none_if_no_db_data():
    with patch("db.market_store.MarketStore") as MockStore:
        MockStore.return_value.get_latest_price.return_value = None
        result = _get_db_fallback_snapshot(offset_pct=0.0)

    assert result is None


def test_get_gold_snapshot_provider_success_returns_fresh():
    saved = []

    def _fake_spot(symbol: str):
        return {
            "GC=F": 4600.0,
            "USDCNY=X": 6.85,
        }.get(symbol)

    with patch("utils.cn_market_provider.fetch_spot", side_effect=_fake_spot), \
         patch("db.market_store.MarketStore") as MockStore:
        MockStore.return_value.save_generic_price.side_effect = (
            lambda *args, **kwargs: saved.append((args, kwargs))
        )
        result = get_gold_snapshot(offset_pct=0.015)

    assert result is not None
    assert result.is_stale is False
    assert result.gold_usd_per_oz == 4600.0
    assert result.usdcny_rate == 6.85
    assert result.bank_cny_per_gram == result.spot_cny_per_gram * 1.015
    assert saved


def test_get_gold_snapshot_falls_back_when_provider_raises():
    with patch("utils.cn_market_provider.fetch_spot", side_effect=ConnectionError("provider down")), \
         patch("db.market_store.MarketStore") as MockStore:
        MockStore.return_value.get_latest_price.side_effect = lambda sym: {
            "GC=F": 4500.0,
            "USDCNY=X": 6.80,
        }.get(sym)

        result = get_gold_snapshot(offset_pct=0.0)

    assert result is not None
    assert result.is_stale is True
    assert result.gold_usd_per_oz == 4500.0


def test_get_gold_snapshot_returns_none_when_all_fail():
    with patch("utils.cn_market_provider.fetch_spot", side_effect=ConnectionError("provider down")), \
         patch("db.market_store.MarketStore") as MockStore:
        MockStore.return_value.get_latest_price.return_value = None
        result = get_gold_snapshot(offset_pct=0.0)

    assert result is None
