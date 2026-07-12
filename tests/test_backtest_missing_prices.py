from __future__ import annotations

import json

import pandas as pd
import pytest

from scripts.backtest_ashare_committee_exit import ExitParams, run_backtest
from scripts.compare_upgrade_profitability import (
    load_history_snapshot,
    paired_block_bootstrap_return_difference,
    write_history_snapshot,
)


def _row(open_price: float, close: float) -> dict:
    return {
        "Open": open_price,
        "High": close,
        "Low": open_price,
        "Close": close,
        "PrevClose": open_price,
        "Volume": 1000.0,
    }


def test_missing_held_bar_carries_last_close_instead_of_resetting_to_cost():
    dates_a = pd.to_datetime(["2026-05-04", "2026-05-05"])
    dates_b = pd.to_datetime(["2026-05-04", "2026-05-05", "2026-05-06"])
    histories = {
        "600000": pd.DataFrame([_row(10.0, 10.0), _row(15.0, 15.0)], index=dates_a),
        "000001": pd.DataFrame([_row(10.0, 10.0), _row(10.0, 10.0), _row(10.0, 10.0)], index=dates_b),
    }
    signal = {
        "verdict": "BUY",
        "score": 90.0,
        "alloc_cny": 10_000.0,
        "atr_pct": 1.0,
        "entry_exit_points": {"buy_pullback_price": 10.0},
    }
    signal_cache = {
        "2026-05-04": {"600000": signal},
        "2026-05-05": {},
        "2026-05-06": {},
    }
    params = ExitParams(
        max_loss_pct=3.0,
        stop_atr_mult=1.0,
        take_profit_r1=100.0,
        take_profit_r2=120.0,
        trailing_atr_mult=1.0,
        min_score_to_buy=60.0,
    )
    common = dict(
        histories=histories,
        names={"600000": "A", "000001": "B"},
        start="2026-05-04",
        end="2026-05-06",
        params=params,
        signal_cache=signal_cache,
        initial_cash=100_000.0,
        fee_rate=0.0005,
    )

    legacy = run_backtest(**common, missing_price_policy="legacy_avg_cost")
    corrected = run_backtest(**common, missing_price_policy="carry_forward")

    assert legacy["metrics"]["final_equity"] == 99_995.0
    assert corrected["metrics"]["final_equity"] == 104_995.0
    assert corrected["diagnostics"]["missing_held_symbol_days"] == 1
    assert corrected["metrics"]["total_fees_cny"] == 5.0
    assert corrected["metrics"]["turnover_pct"] > 0


def test_paired_block_bootstrap_is_zero_for_identical_paths():
    curve = [
        {"date": f"2026-05-{day:02d}", "equity": 100_000 + day * 100}
        for day in range(1, 26)
    ]
    result = paired_block_bootstrap_return_difference(curve, curve, samples=250, seed=1)
    assert result["available"] is True
    assert result["mean_difference_pp"] == 0.0
    assert result["confidence_interval_95_pp"] == [0.0, 0.0]


def test_frozen_history_snapshot_round_trip_preserves_digest(tmp_path):
    index = pd.to_datetime(["2026-05-04", "2026-05-05"])
    histories = {
        "600000": pd.DataFrame(
            [_row(10.0, 10.2), _row(10.2, 10.5)],
            index=index,
        )
    }
    path = tmp_path / "market_snapshot.json"

    written = write_history_snapshot(path, histories, period="2y")
    loaded, payload = load_history_snapshot(path, expected_symbols=["600000"])

    pd.testing.assert_frame_equal(
        loaded["600000"],
        histories["600000"].loc[:, loaded["600000"].columns],
        check_freq=False,
        check_names=False,
    )
    assert payload["manifest"] == written["manifest"]


def test_frozen_history_snapshot_rejects_tampered_price(tmp_path):
    index = pd.to_datetime(["2026-05-04", "2026-05-05"])
    histories = {
        "600000": pd.DataFrame(
            [_row(10.0, 10.2), _row(10.2, 10.5)],
            index=index,
        )
    }
    path = tmp_path / "market_snapshot.json"
    write_history_snapshot(path, histories, period="2y")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["histories"]["600000"][0][4] = 999.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256"):
        load_history_snapshot(path, expected_symbols=["600000"])
