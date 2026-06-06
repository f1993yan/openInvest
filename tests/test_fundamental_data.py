import pandas as pd

from utils import fundamental_data


def test_extract_financial_indicator_ratios():
    df = pd.DataFrame([
        {
            "日期": "2024-12-31",
            "净资产收益率(%)": "18.0",
            "销售毛利率(%)": "45.5",
            "营业利润率(%)": "16.2",
            "营业收入增长率(%)": "25.0",
            "资产负债率(%)": "38.0",
            "流动比率": "1.8",
        }
    ])

    metrics, sources = fundamental_data._extract_from_financial_indicator(df)

    assert metrics["roe"] == 0.18
    assert metrics["gross_margin"] == 0.455
    assert metrics["operating_margin"] == 0.162
    assert metrics["revenue_growth"] == 0.25
    assert metrics["debt_to_assets"] == 0.38
    assert metrics["current_ratio"] == 1.8
    assert "roe" in sources


def test_fundamental_snapshot_uses_fresh_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fundamental_data, "FUNDAMENTAL_CACHE_DIR", tmp_path)
    snapshot = {
        "symbol": "600900",
        "market": "a",
        "fetched_at": "2099-01-01T00:00:00",
        "metrics": {"roe": 0.12},
        "metric_sources": {"roe": "fixture"},
        "warnings": [],
        "source": "fixture",
    }
    fundamental_data._write_cache("600900", snapshot)

    out = fundamental_data.get_fundamental_snapshot("600900", "a")

    assert out["metrics"]["roe"] == 0.12
    assert out["cache_hit"] is True
    assert out["cache_stale"] is False


def test_get_fundamental_snapshot_returns_stale_cache_on_fetch_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(fundamental_data, "FUNDAMENTAL_CACHE_DIR", tmp_path)
    snapshot = {
        "symbol": "600900",
        "market": "a",
        "fetched_at": "2000-01-01T00:00:00",
        "metrics": {"roe": 0.11},
        "metric_sources": {"roe": "fixture"},
        "warnings": [],
        "source": "fixture",
    }
    fundamental_data._write_cache("600900", snapshot)

    def _boom(symbol, market):
        raise RuntimeError("network down")

    monkeypatch.setattr(fundamental_data, "fetch_fundamental_snapshot", _boom)

    out = fundamental_data.get_fundamental_snapshot("600900", "a")

    assert out["metrics"]["roe"] == 0.11
    assert out["cache_hit"] is True
    assert out["cache_stale"] is True
    assert out["warnings"] == ["live_fetch_failed:RuntimeError"]
