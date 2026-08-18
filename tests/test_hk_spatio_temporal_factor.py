from __future__ import annotations

import json

import numpy as np
import pandas as pd

from core.hk_spatio_temporal_factor import (
    DEFAULT_HK_REFERENCE_UNIVERSE,
    assess_hk_spatio_universe,
    score_hk_spatio_features,
)
from jobs.market_monitor_quotes import build_hk_spatio_factor_context


def _history(seed: int, drift: float, *, periods: int = 380) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    returns = drift + rng.normal(0.0, 0.009, periods)
    close = 20.0 * np.cumprod(1.0 + returns)
    return pd.DataFrame(
        {"Close": close, "Volume": 1_000_000.0},
        index=pd.bdate_range("2024-01-02", periods=periods),
    )


def test_production_hsi_reference_universe_is_complete_and_unique():
    assert len(DEFAULT_HK_REFERENCE_UNIVERSE) == 93
    assert len(set(DEFAULT_HK_REFERENCE_UNIVERSE)) == 93


def test_spatio_score_uses_the_frozen_backtest_formula():
    features = pd.DataFrame(
        {
            "ret20": [0.08, 0.03, -0.01, 0.05],
            "ret60": [0.18, 0.09, 0.04, 0.12],
            "ret120": [0.25, 0.12, 0.08, 0.16],
            "vol20": [0.20, 0.16, 0.14, 0.25],
            "vol60": [0.22, 0.18, 0.15, 0.24],
        },
        index=["00001", "00002", "00003", "00004"],
    )
    scored = score_hk_spatio_features(features)
    temporal = (
        features["ret20"] / features["vol20"].clip(lower=0.08)
        + features["ret60"] / features["vol60"].clip(lower=0.08)
        + features["ret120"] / features["vol60"].clip(lower=0.08)
    ) / 3.0
    expected = (
        0.55 * temporal.rank(method="average", pct=True)
        + 0.45 * features["ret60"].rank(method="average", pct=True)
    )

    assert scored.loc["00001", "spatio_score"] == expected.loc["00001"]
    assert scored.loc["00003", "spatio_score"] == 0.0


def test_hk_assessment_is_strictly_point_in_time():
    histories = {f"{i + 1:05d}": _history(i, 0.0002 + i * 0.00015) for i in range(6)}
    cutoff = histories["00001"].index[-31]
    before = assess_hk_spatio_universe(histories, as_of=cutoff)
    changed = {symbol: frame.copy() for symbol, frame in histories.items()}
    for frame in changed.values():
        frame.loc[frame.index > cutoff, "Close"] *= 5.0
    after = assess_hk_spatio_universe(changed, as_of=cutoff)

    assert {symbol: value.as_dict() for symbol, value in before.items()} == {
        symbol: value.as_dict() for symbol, value in after.items()
    }


def test_hk_universe_selects_top_four_with_capped_inverse_volatility_weights():
    histories = {f"{i + 1:05d}": _history(i, 0.0001 + i * 0.0002) for i in range(8)}
    assessments = assess_hk_spatio_universe(histories)
    selected = [value for value in assessments.values() if value.selected]

    assert 1 <= len(selected) <= 4
    assert all(value.eligible for value in selected)
    assert all(0 < value.target_weight_pct <= 35.0 for value in selected)
    assert sum(value.target_weight_pct for value in selected) <= 95.01
    assert all(not value.low_confidence for value in assessments.values())
    assert all(value.sample_size >= 20 for value in assessments.values())


def test_short_hk_history_is_explicitly_low_confidence():
    assessments = assess_hk_spatio_universe({"00700": _history(1, 0.001, periods=100)})

    assert assessments["00700"].low_confidence is True
    assert assessments["00700"].reason == "insufficient_history"


def test_monitor_uses_public_reference_pool_but_returns_only_requested_rows(monkeypatch, tmp_path):
    import core.hk_spatio_temporal_factor as factor_module

    reference = ("00001", "00002", "00003", "00700")
    histories = {
        symbol: _history(index, 0.0003 + index * 0.0002)
        for index, symbol in enumerate(reference)
    }
    fetched = []

    def fake_history(symbol, period):
        fetched.append(symbol)
        return histories[symbol]

    monkeypatch.setattr(factor_module, "DEFAULT_HK_REFERENCE_UNIVERSE", reference)
    monkeypatch.setattr("utils.market_data_provider.get_history_data", fake_history)
    context = build_hk_spatio_factor_context(
        [{"symbol": "00700", "market": "hk"}],
        state_path=tmp_path / "hk_spatio_state.json",
    )

    assert set(fetched) == set(reference)
    assert set(context) == {"00700"}
    assert context["00700"]["universe_size"] == 4
    assert context["00700"]["low_confidence"] is False


def test_hk_targets_are_frozen_until_twenty_completed_sessions(monkeypatch, tmp_path):
    base = {f"{i + 1:05d}": _history(i, 0.0002 + i * 0.0002) for i in range(6)}
    active = dict(base)

    def fake_history(symbol, period):
        return active[symbol]

    monkeypatch.setattr("utils.market_data_provider.get_history_data", fake_history)
    stocks = [{"symbol": symbol, "market": "hk"} for symbol in base]
    state = tmp_path / "hk_spatio_state.json"
    first = build_hk_spatio_factor_context(
        stocks, state_path=state, reference_symbols=(),
    )
    first_targets = {
        symbol: (row["selected"], row["target_weight_pct"])
        for symbol, row in first.items()
    }

    def extended(extra_sessions: int) -> dict[str, pd.DataFrame]:
        output = {}
        for symbol, frame in base.items():
            multiplier = 1.08 if symbol == "00001" else 0.998
            output[symbol] = pd.concat([
                frame,
                pd.DataFrame(
                    {
                        "Close": [float(frame["Close"].iloc[-1]) * multiplier ** (i + 1) for i in range(extra_sessions)],
                        "Volume": [1_000_000.0] * extra_sessions,
                    },
                    index=pd.bdate_range(
                        frame.index[-1] + pd.Timedelta(days=1),
                        periods=extra_sessions,
                    ),
                ),
            ])
        return output

    active = extended(19)
    frozen = build_hk_spatio_factor_context(
        stocks, state_path=state, reference_symbols=(),
    )
    frozen_targets = {
        symbol: (row["selected"], row["target_weight_pct"])
        for symbol, row in frozen.items()
    }
    assert frozen_targets == first_targets
    assert all("target_membership_frozen_from_" in row["reason"] for row in frozen.values())

    active = extended(20)
    refreshed = build_hk_spatio_factor_context(
        stocks, state_path=state, reference_symbols=(),
    )
    saved = json.loads(state.read_text(encoding="utf-8"))

    assert saved["model"] == "hk_spatio_temporal_momentum_proxy_v1"
    assert saved["rebalance_sessions"] == 20
    assert saved["represents_account_holdings"] is False
    assert all("target_membership_frozen_from_" not in row["reason"] for row in refreshed.values())
