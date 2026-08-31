from __future__ import annotations

import numpy as np
import pandas as pd

from core.ashare_behavioral_factor import (
    assess_behavioral_universe,
    build_behavioral_feature_table,
    score_behavioral_features,
)
from jobs.market_monitor_quotes import build_behavioral_factor_context


def _history(seed: int, drift: float, *, periods: int = 380) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    returns = drift + rng.normal(0.0, 0.009, periods)
    close = 20.0 * np.cumprod(1.0 + returns)
    volume = 1_000_000 * (1.0 + rng.normal(0.0, 0.08, periods))
    return pd.DataFrame(
        {"Close": close, "Volume": volume},
        index=pd.bdate_range("2025-01-02", periods=periods),
    )


def test_behavioral_formula_uses_documented_component_weights():
    features = pd.DataFrame(
        {
            "ret20": [0.05, 0.01, -0.02, 0.03],
            "ret60": [0.12, 0.06, -0.04, 0.08],
            "ret120": [0.20, 0.08, -0.10, 0.10],
            "ret252": [0.30, 0.10, -0.15, 0.16],
            "vol20": [0.15, 0.30, 0.20, 0.25],
            "volume_ratio20_120": [0.7, 1.2, 0.9, 1.0],
            "above_ema100": [0.08, 0.02, -0.03, 0.04],
        },
        index=["A", "B", "C", "D"],
    )
    scored = score_behavioral_features(features)
    expected = (
        0.65 * scored.loc["A", "momentum_score"]
        + 0.25 * scored.loc["A", "low_turnover_score"]
        + 0.10 * scored.loc["A", "low_volatility_score"]
    )

    assert scored.loc["A", "behavioral_score"] == expected
    assert pd.isna(scored.loc["C", "behavioral_score"])


def test_assessment_is_strictly_point_in_time():
    histories = {str(600000 + i): _history(i, 0.0002 + i * 0.00015) for i in range(6)}
    cutoff = histories["600000"].index[-31]
    before = assess_behavioral_universe(histories, as_of=cutoff)
    changed = {symbol: frame.copy() for symbol, frame in histories.items()}
    for frame in changed.values():
        frame.loc[frame.index > cutoff, "Close"] *= 5.0
        frame.loc[frame.index > cutoff, "Volume"] *= 10.0
    after = assess_behavioral_universe(changed, as_of=cutoff)

    assert {key: value.as_dict() for key, value in before.items()} == {
        key: value.as_dict() for key, value in after.items()
    }


def test_behavioral_universe_selects_at_most_four_and_caps_weights():
    histories = {str(600000 + i): _history(i, 0.0001 + i * 0.0002) for i in range(8)}
    assessments = assess_behavioral_universe(histories)
    selected = [value for value in assessments.values() if value.selected]

    assert 1 <= len(selected) <= 4
    assert all(value.eligible for value in selected)
    assert all(0 < value.target_weight_pct <= 35.0 for value in selected)
    assert sum(value.target_weight_pct for value in selected) <= 95.01
    assert all(not value.low_confidence for value in assessments.values())
    assert all(value.trailing_3m_sample_size >= 40 for value in assessments.values())
    assert len({value.optimizer_weight for value in assessments.values()}) > 1
    assert all(0.55 <= value.optimizer_weight <= 1.0 for value in assessments.values())


def test_less_than_252_sessions_is_low_confidence():
    assessments = assess_behavioral_universe({"600000": _history(1, 0.001, periods=200)})

    assert assessments["600000"].low_confidence is True
    assert assessments["600000"].reason == "insufficient_history"


def test_feature_table_accepts_lowercase_columns():
    frame = _history(1, 0.001).rename(columns={"Close": "close", "Volume": "volume"})
    table = build_behavioral_feature_table({"600000": frame})

    assert "600000" in table.index
    assert np.isfinite(table.loc["600000", "ret252"])


def test_monitor_target_membership_is_frozen_until_five_sessions(monkeypatch, tmp_path):
    base = {str(600000 + i): _history(i, 0.0002 + i * 0.0002, periods=360) for i in range(6)}
    active = dict(base)

    def fake_history(symbol, period):
        return active[symbol]

    monkeypatch.setattr("utils.market_data_provider.get_history_data", fake_history)
    stocks = [{"symbol": symbol, "market": "a"} for symbol in base]
    state = tmp_path / "behavioral_state.json"
    first = build_behavioral_factor_context(stocks, state_path=state)
    first_targets = {
        symbol: (row["selected"], row["target_weight_pct"])
        for symbol, row in first.items()
    }
    legacy_state = __import__("json").loads(state.read_text(encoding="utf-8"))
    for key in ("selection_scope", "represents_account_holdings", "selection_semantics"):
        legacy_state.pop(key, None)
    state.write_text(__import__("json").dumps(legacy_state), encoding="utf-8")

    active = {
        symbol: pd.concat([
            frame,
            pd.DataFrame(
                {
                    "Close": [float(frame["Close"].iloc[-1]) * (1.20 if symbol == "600000" else 0.99) ** (i + 1) for i in range(4)],
                    "Volume": [900_000.0] * 4,
                },
                index=pd.bdate_range(frame.index[-1] + pd.Timedelta(days=1), periods=4),
            ),
        ])
        for symbol, frame in base.items()
    }
    frozen = build_behavioral_factor_context(stocks, state_path=state)
    frozen_targets = {
        symbol: (row["selected"], row["target_weight_pct"])
        for symbol, row in frozen.items()
    }

    assert frozen_targets == first_targets
    assert all("target_membership_frozen_from_" in row["reason"] for row in frozen.values())
    assert all(row["selection_scope"] == "factor_model_target_portfolio" for row in frozen.values())
    assert all(row["represents_account_holding"] is False for row in frozen.values())
    migrated = __import__("json").loads(state.read_text(encoding="utf-8"))
    assert migrated["selection_scope"] == "factor_model_target_portfolio"
    assert migrated["represents_account_holdings"] is False

    active = {
        symbol: pd.concat([
            frame,
            pd.DataFrame(
                {
                    "Close": [float(frame["Close"].iloc[-1]) * (1.20 if symbol == "600000" else 0.99) ** (i + 1) for i in range(5)],
                    "Volume": [900_000.0] * 5,
                },
                index=pd.bdate_range(frame.index[-1] + pd.Timedelta(days=1), periods=5),
            ),
        ])
        for symbol, frame in base.items()
    }
    refreshed = build_behavioral_factor_context(stocks, state_path=state)
    saved = __import__("json").loads(state.read_text(encoding="utf-8"))

    assert saved["rebalance_sessions"] == 5
    assert saved["rebalance_date"] == str(max(frame.index[-1] for frame in active.values()))[:10]
    assert saved["score_updated_at"] == saved["targets_effective_at"]
    assert saved["targets_effective_at"].endswith("+08:00")
    assert saved["selection_scope"] == "factor_model_target_portfolio"
    assert saved["represents_account_holdings"] is False
    assert all("target_membership_frozen_from_" not in row["reason"] for row in refreshed.values())
