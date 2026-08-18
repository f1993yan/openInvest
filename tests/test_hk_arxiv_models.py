from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.compare_hk_arxiv_models import (
    DEFAULT_HK_UNIVERSE,
    ModelSpec,
    _buy_hold_scores,
    build_model_scores,
    estimate_big_trader_strengths,
    fuzzy_excess_demands,
    run_independent_sleeves,
    run_cross_section,
)


def _history(prices: np.ndarray) -> pd.DataFrame:
    index = pd.bdate_range("2025-01-01", periods=len(prices))
    close = pd.Series(prices, index=index, dtype=float)
    return pd.DataFrame({
        "Open": close,
        "High": close * 1.01,
        "Low": close * 0.99,
        "Close": close,
        "Volume": 1_000_000.0,
    })


def test_frozen_hsi_universe_is_complete_and_unique():
    assert len(DEFAULT_HK_UNIVERSE) == 93
    assert len(set(DEFAULT_HK_UNIVERSE)) == len(DEFAULT_HK_UNIVERSE)


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        (-0.04, (0.0, 0.4)),
        (-0.025, (0.0, 0.3)),
        (-0.01, (0.0, 0.1)),
        (0.0, (0.0, 0.0)),
        (0.01, (-0.1, 0.0)),
        (0.025, (-0.3, 0.0)),
        (0.04, (-0.4, 0.0)),
    ],
)
def test_fuzzy_excess_demands_match_paper_piecewise_rules(x: float, expected: tuple[float, float]):
    actual = fuzzy_excess_demands(x, w=0.01)
    assert actual == pytest.approx(expected)


def test_rls_estimate_has_no_values_before_an_observed_return():
    close = pd.Series([10.0, 10.1, 10.2, 10.3, 10.4], index=pd.bdate_range("2025-01-01", periods=5))
    result = estimate_big_trader_strengths(close, n=3)
    assert result.iloc[:3].isna().all().all()
    assert result.iloc[3].notna().all()


def test_sleeve_executes_yesterdays_signal_at_todays_open_and_charges_fees():
    history = _history(np.array([10.0, 10.0, 11.0, 12.0, 13.0]))
    calendar = history.index
    scores = pd.DataFrame({"00001": [0.0, 1.0, 0.0, 0.0, 0.0]}, index=calendar)
    result = run_independent_sleeves(
        spec=ModelSpec("test", "test", "independent_sleeves"),
        histories={"00001": history},
        scores=scores,
        calendar=calendar,
        start=calendar[1].strftime("%Y-%m-%d"),
        end=calendar[-1].strftime("%Y-%m-%d"),
        initial_cash=1_000.0,
        fee_rate=0.002,
    )
    assert [(row["date"], row["side"]) for row in result["trades"]] == [
        (calendar[2].strftime("%Y-%m-%d"), "BUY"),
        (calendar[3].strftime("%Y-%m-%d"), "SELL"),
    ]
    assert result["trades"][0]["price"] == 11.0
    assert result["metrics"]["total_fees_hkd"] > 0.0


def test_execution_day_close_cannot_change_execution_day_order():
    history = _history(np.linspace(10.0, 15.0, 8))
    calendar = history.index
    scores = pd.DataFrame({"00001": [0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]}, index=calendar)
    common = dict(
        spec=ModelSpec("test", "test", "independent_sleeves"),
        scores=scores,
        calendar=calendar,
        start=calendar[1].strftime("%Y-%m-%d"),
        end=calendar[-1].strftime("%Y-%m-%d"),
        initial_cash=1_000.0,
        fee_rate=0.0,
    )
    original = run_independent_sleeves(histories={"00001": history}, **common)
    mutated = history.copy()
    mutated.loc[calendar[2], "Close"] = 9999.0
    changed = run_independent_sleeves(histories={"00001": mutated}, **common)
    assert original["trades"][0] == changed["trades"][0]


def test_buy_and_hold_signal_survives_a_long_suspension():
    calendar = pd.bdate_range("2025-01-01", periods=15)
    history = _history(np.linspace(10.0, 11.0, 15)).drop(calendar[3:12])
    scores = _buy_hold_scores({"00001": history}, calendar)
    assert (scores.loc[calendar[2]:, "00001"] == 1.0).all()


def test_cross_section_execution_does_not_read_execution_day_close():
    histories = {
        f"{symbol:05d}": _history(np.linspace(10.0 + symbol, 20.0 + 2 * symbol, 180))
        for symbol in range(1, 6)
    }
    scores, vol20, calendar = build_model_scores(histories)
    execution_day = calendar[-2]
    spec = ModelSpec("spatio_temporal", "test", "cross_section", rebalance_days=20)
    common = dict(
        spec=spec,
        vol20=vol20,
        calendar=calendar,
        start=execution_day.strftime("%Y-%m-%d"),
        end=calendar[-1].strftime("%Y-%m-%d"),
        initial_cash=100_000.0,
        fee_rate=0.0,
    )
    original = run_cross_section(histories=histories, scores=scores["spatio_temporal"], **common)

    mutated_histories = {symbol: frame.copy() for symbol, frame in histories.items()}
    mutated_histories["00001"].loc[execution_day, "Close"] = 9999.0
    changed_scores, changed_vol20, changed_calendar = build_model_scores(mutated_histories)
    changed = run_cross_section(
        histories=mutated_histories,
        scores=changed_scores["spatio_temporal"],
        vol20=changed_vol20,
        calendar=changed_calendar,
        start=execution_day.strftime("%Y-%m-%d"),
        end=calendar[-1].strftime("%Y-%m-%d"),
        initial_cash=100_000.0,
        fee_rate=0.0,
        spec=spec,
    )
    original_first_day = [row for row in original["trades"] if row["date"] == execution_day.strftime("%Y-%m-%d")]
    changed_first_day = [row for row in changed["trades"] if row["date"] == execution_day.strftime("%Y-%m-%d")]
    assert original_first_day
    assert original_first_day == changed_first_day
