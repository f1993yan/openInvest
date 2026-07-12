from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.compare_arxiv_factors import (
    FACTOR_SPECS,
    FactorSpec,
    attach_robustness_windows,
    build_feature_table,
    factor_target_weights,
    run_factor_backtest,
)


def _history(close_values: np.ndarray, start: str = "2025-01-01") -> pd.DataFrame:
    index = pd.bdate_range(start, periods=len(close_values))
    close = pd.Series(close_values, index=index, dtype=float)
    frame = pd.DataFrame({
        "Open": close,
        "High": close * 1.01,
        "Low": close * 0.99,
        "Close": close,
        "Volume": np.linspace(1_000_000, 1_200_000, len(close)),
    })
    frame["PrevClose"] = frame["Close"].shift(1)
    return frame.dropna()


def test_feature_table_never_reads_execution_day_bar():
    history = _history(np.linspace(10.0, 20.0, 300))
    decision_date = history.index[-1]
    original = build_feature_table({"600000": history}, decision_date)

    mutated = history.copy()
    mutated.loc[decision_date, ["Open", "High", "Low", "Close"]] = 9999.0
    changed = build_feature_table({"600000": mutated}, decision_date)

    pd.testing.assert_frame_equal(original, changed)
    assert original.at["600000", "close"] == history.loc[history.index < decision_date, "Close"].iloc[-1]


def test_inverse_volatility_weights_respect_single_name_cap():
    symbols = ["600000", "600001", "600002", "600003"]
    features = pd.DataFrame({
        "ema_return100": [0.02, 0.018, 0.016, 0.014],
        "above_ema100": [0.10, 0.09, 0.08, 0.07],
        "vol20": [0.08, 0.12, 0.25, 0.40],
    }, index=symbols)
    spec = next(item for item in FACTOR_SPECS if item.key == "ema_trend")

    weights = factor_target_weights(spec, features)

    assert len(weights) == 4
    assert sum(weights.values()) <= spec.gross_exposure + 1e-9
    assert max(weights.values()) <= spec.max_weight + 1e-9


def test_factor_backtest_obeys_lots_and_charges_fees():
    histories = {
        "600000": _history(np.linspace(10.0, 18.0, 320)),
        "600001": _history(np.linspace(12.0, 19.0, 320)),
        "688001": _history(np.linspace(20.0, 31.0, 320)),
        "300001": _history(np.linspace(8.0, 15.0, 320)),
    }
    dates = sorted(set().union(*(set(frame.index) for frame in histories.values())))
    start = dates[-20].strftime("%Y-%m-%d")
    end = dates[-1].strftime("%Y-%m-%d")
    spec = FactorSpec("ema_trend", "test", rebalance_days=5, top_k=4)

    result = run_factor_backtest(
        spec=spec,
        histories=histories,
        start=start,
        end=end,
        initial_cash=100_000.0,
        fee_rate=0.0005,
    )

    buys = [row for row in result["trades"] if row["side"] == "BUY"]
    assert buys
    assert all(row["shares"] % 100 == 0 for row in result["trades"])
    assert next(row for row in buys if row["symbol"] == "688001")["shares"] >= 200
    assert result["metrics"]["total_fees_cny"] > 0
    assert result["metrics"]["average_exposure_pct"] >= 20.0
    assert len({(row["date"], row["symbol"]) for row in result["trades"]}) == len(result["trades"])


def test_robust_ranking_rejects_one_window_winner():
    def window(stable_return: float, fragile_return: float, baseline: float = 0.0) -> dict:
        def row(key: str, value: float) -> dict:
            return {
                "factor": {"key": key, "label": key},
                "metrics": {
                    "total_return_pct": value,
                    "max_drawdown_pct": -2.0,
                    "turnover_pct": 100.0,
                    "average_exposure_pct": 60.0,
                },
            }

        return {
            "period": {"start": "2026-01-01", "end": "2026-02-28"},
            "current_baseline": {"total_return_pct": baseline},
            "equal_weight_buy_hold": {"total_return_pct": 1.0},
            "candidate_summary": [row("stable", stable_return), row("fragile", fragile_return)],
        }

    current = window(5.0, 30.0)
    prior = [window(4.0, -5.0), window(3.0, 0.0), window(6.0, -2.0)]

    result = attach_robustness_windows(current, prior)

    assert result["robust_top_three"][0]["factor"]["key"] == "stable"
    fragile = next(row for row in result["robustness_summary"] if row["factor"]["key"] == "fragile")
    assert fragile["robust_eligible"] is False
