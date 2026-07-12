from __future__ import annotations

import pandas as pd

from scripts.compare_arxiv_factors import FactorSpec, factor_target_weights
from scripts.compare_hybrid_factor import _prior_windows, _resolve_trading_window


def test_hybrid_weights_are_investable_and_capped():
    symbols = ["600000", "600001", "600002", "600003"]
    features = pd.DataFrame({
        "ret5": [-0.01, 0.00, 0.01, 0.02],
        "ret20": [0.08, 0.07, 0.06, 0.05],
        "ret60": [0.20, 0.18, 0.16, 0.14],
        "ret120": [0.30, 0.28, 0.26, 0.24],
        "ret252": [0.40, 0.38, 0.36, 0.34],
        "vol20": [0.10, 0.15, 0.20, 0.25],
        "vol60": [0.12, 0.17, 0.22, 0.27],
        "above_ema100": [0.10, 0.09, 0.08, 0.07],
        "volume_ratio20_120": [0.8, 0.9, 1.0, 1.1],
    }, index=symbols)
    spec = FactorSpec(
        "hybrid_test",
        "test",
        rebalance_days=20,
        top_k=4,
        spatio_weight=0.70,
        target_blend=0.50,
        min_trade_weight=0.05,
    )

    weights = factor_target_weights(spec, features)

    assert len(weights) == 4
    assert 0 < sum(weights.values()) <= spec.gross_exposure + 1e-9
    assert max(weights.values()) <= spec.max_weight + 1e-9


def test_training_windows_are_non_overlapping_and_before_holdout():
    windows = _prior_windows("2026-05-13", "2026-07-12", 3)

    assert len(windows) == 3
    assert windows[-1]["end"] == "2026-05-12"
    for previous, current in zip(windows, windows[1:]):
        assert pd.Timestamp(previous["end"]) < pd.Timestamp(current["start"])
    assert all(pd.Timestamp(window["end"]) < pd.Timestamp("2026-05-13") for window in windows)


def test_resolve_trading_window_uses_exact_market_day_count():
    index = pd.bdate_range("2025-01-01", periods=120)
    histories = {
        "600000": pd.DataFrame({"Close": range(120)}, index=index),
        "000001": pd.DataFrame({"Close": range(120)}, index=index),
    }

    start, end = _resolve_trading_window(
        histories,
        end="2026-12-31",
        trading_days=82,
    )

    selected = index[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]
    assert len(selected) == 82
    assert pd.Timestamp(end) == index[-1]
