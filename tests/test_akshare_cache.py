from datetime import datetime, timedelta
import sys
import types

import pandas as pd
import pytest

from utils.akshare_data import (
    _apply_period_filter,
    _cache_is_fresh,
    adjustment_splice_diagnostics,
)


def test_akshare_cache_freshness_uses_latest_cached_date():
    fresh = pd.DataFrame(
        {"Close": [1.0] * 150},
        index=pd.to_datetime([(datetime.now() - timedelta(days=i)).date() for i in range(150)][::-1]),
    )
    stale = pd.DataFrame(
        {"Close": [1.0] * 150},
        index=pd.to_datetime([(datetime.now() - timedelta(days=30 + i)).date() for i in range(150)][::-1]),
    )

    assert _cache_is_fresh(fresh)
    assert not _cache_is_fresh(stale)


def test_akshare_period_filter_keeps_requested_window():
    old = datetime.now() - timedelta(days=120)
    recent = datetime.now() - timedelta(days=10)
    df = pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.to_datetime([old, recent]))

    filtered = _apply_period_filter(df, "1mo")

    assert list(filtered["Close"]) == [2.0]


def test_adjustment_splice_detects_uniform_log_price_shift():
    dates = pd.date_range("2026-01-01", periods=40, freq="B")
    cached = pd.DataFrame({"Close": [100 + i for i in range(40)]}, index=dates)
    fresh = pd.DataFrame({"Close": cached["Close"] * 1.025}, index=dates)

    diagnostics = adjustment_splice_diagnostics(cached, fresh)

    assert diagnostics["detected"] is True
    assert diagnostics["overlap"] == 40
    assert diagnostics["median_shift_pct"] == pytest.approx(2.5, abs=1e-8)
    assert diagnostics["consistent_fraction"] == 1.0


def test_adjustment_splice_rejects_nonuniform_provider_noise():
    dates = pd.date_range("2026-01-01", periods=40, freq="B")
    cached = pd.DataFrame({"Close": [100.0] * 40}, index=dates)
    ratios = [1.02 if i % 2 else 0.98 for i in range(40)]
    fresh = pd.DataFrame({"Close": [100.0 * ratio for ratio in ratios]}, index=dates)

    diagnostics = adjustment_splice_diagnostics(cached, fresh)

    assert diagnostics["detected"] is False


def test_history_refresh_replaces_whole_symbol_when_basis_changes(monkeypatch):
    import utils.akshare_data as ak

    dates = pd.date_range(datetime.now() - timedelta(days=220), periods=160, freq="B")
    cached = pd.DataFrame({"Close": [100.0] * 160}, index=dates)
    fresh = pd.DataFrame(
        {
            "Open": [102.0] * 160,
            "High": [103.0] * 160,
            "Low": [101.0] * 160,
            "Close": [102.0] * 160,
            "Volume": [1000.0] * 160,
        },
        index=dates,
    )
    full = pd.concat([fresh.iloc[:20], fresh]).sort_index().loc[lambda frame: ~frame.index.duplicated(keep="last")]

    class FakeStore:
        def __init__(self):
            self.replacements = []

        def get_history_df(self, symbol, days=730):
            return cached.copy()

        def save_generic_price(self, *args, **kwargs):
            raise AssertionError("basis shift must not merge row-by-row")

        def replace_generic_history(self, symbol, rows, source):
            self.replacements.append((symbol, list(rows), source))

    store = FakeStore()
    monkeypatch.setattr(ak, "_STORE", store)
    monkeypatch.setattr(ak, "_cache_is_fresh", lambda df: False)
    monkeypatch.setattr(ak, "_is_index", lambda symbol: False)
    monkeypatch.setattr(ak, "_is_a_share", lambda symbol: True)
    monkeypatch.setattr(ak, "_fetch_a_share", lambda symbol, period: full.copy() if period == "2y" else fresh.copy())

    result = ak.get_history_data("600000", "1y")

    assert not result.empty
    assert len(store.replacements) == 1
    assert store.replacements[0][0] == "600000"
    assert "adjustment_basis_refresh" in store.replacements[0][2]


def test_hk_history_prefers_tencent_route_before_akshare(monkeypatch):
    import utils.akshare_data as ak

    expected = pd.DataFrame(
        {"Close": [10.0, 10.5]},
        index=pd.date_range("2026-01-01", periods=2),
    )
    fake_cn = types.ModuleType("utils.cn_market_provider")
    fake_cn.fetch_history = lambda symbol, period: expected.copy()
    fake_akshare = types.ModuleType("akshare")

    def fail_akshare(**kwargs):
        raise AssertionError("AkShare fallback must not run when Tencent data is available")

    fake_akshare.stock_hk_daily = fail_akshare
    monkeypatch.setitem(sys.modules, "utils.cn_market_provider", fake_cn)
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)

    result = ak._fetch_hk_stock("00700", "2y")

    pd.testing.assert_frame_equal(result, expected)

