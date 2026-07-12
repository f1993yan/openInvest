from __future__ import annotations

import pandas as pd

from core.buy_signal_miner import buy_signal_summary_text, mine_historical_buy_signals
from core.decision_synthesis import synthesize_decision


def _breakout_df() -> pd.DataFrame:
    rows = []
    close = 10.0
    for i in range(80):
        open_price = close
        close = close + 0.03
        high = close + 0.06
        low = open_price - 0.05
        volume = 1000 + i * 3
        rows.append((open_price, high, low, close, volume))
    for idx in (30, 55):
        close_signal = max(row[1] for row in rows[max(0, idx - 20):idx]) * 1.08
        rows[idx] = (close_signal * 0.99, close_signal * 1.015, close_signal * 0.97, close_signal, 2800 + idx * 10)
        for j in range(idx + 1, min(idx + 6, len(rows))):
            prev = rows[j - 1][3]
            rows[j] = (prev, prev * 1.03, prev * 0.995, prev * 1.02, 1500 + j * 5)
    close_signal = max(row[1] for row in rows[-21:-1]) * 1.08
    rows[79] = (close_signal * 0.99, close_signal * 1.015, close_signal * 0.97, close_signal, 4200)
    idx = pd.date_range("2026-03-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def test_mine_historical_buy_signals_detects_latest_breakout_and_backtests_history():
    summary = mine_historical_buy_signals("603308", _breakout_df())

    assert summary.latest_signal is not None
    assert summary.latest_signal.trade_date == "2026-05-19"
    assert summary.latest_signal.signal_type == "volume_breakout"
    assert summary.latest_signal_age_days == 0
    assert summary.sample_size >= 3
    assert summary.hit_rate_5d is not None
    assert summary.expected_score > 50

    text = buy_signal_summary_text(summary)
    assert "系统回测信号" in text
    assert "2026-05-19" in text


def test_mine_historical_buy_signals_handles_insufficient_history():
    df = _breakout_df().tail(20)
    summary = mine_historical_buy_signals("603308", df)

    assert summary.latest_signal is None
    assert summary.confidence == "low"
    assert "不足" in summary.warning


def test_decision_synthesis_uses_guarded_final_action_over_optimizer_raw_action():
    class _Opt:
        verdict = "BUY"
        alloc_cny = 3000
        confidence = 0.82
        expected_return_pct = 4.2
        p_directional = 0.63

    synthesis = synthesize_decision(
        symbol="603308",
        optimizer=_Opt(),
        parsed={"verdict": "HOLD", "confidence": 0.55, "alloc_cny": 0},
        entry_exit_points={"reward_risk_ratio": 1.8, "buy_pullback_price": 10.0, "buy_breakout_price": 11.2},
        right_side_gate={"allow": False, "reason": "right_side_not_confirmed"},
        optimizer_review="CONCLUSION: caution\nONE_LINE: 等突破确认",
        current_price=10.8,
    )

    assert synthesis.final_action == "HOLD"
    assert synthesis.action_label == "观察/持有"
    assert any("优化器原始动作" in item for item in synthesis.conflicts)


def test_decision_synthesis_surfaces_a_share_behavioral_parameters_first():
    class _Opt:
        verdict = "ACCUMULATE"
        alloc_cny = 5000
        confidence = 0.78
        expected_return_pct = 3.2
        p_directional = 0.61
        behavioral_model = "a_share_behavioral_v1"
        behavioral_factor_score = 82.4
        behavioral_target_weight_pct = 24.6
        behavioral_selected = True
        behavioral_low_confidence = False
        behavioral_optimizer_weight = 0.81
        behavioral_trailing_3m_return_pct = 6.2
        behavioral_trailing_3m_hit_rate = 0.57
        behavioral_trailing_3m_sample_size = 63

    synthesis = synthesize_decision(
        symbol="600900",
        optimizer=_Opt(),
        parsed={"verdict": "ACCUMULATE", "confidence": 0.78, "alloc_cny": 5000},
        entry_exit_points={"reward_risk_ratio": 1.9},
        right_side_gate={"allow": True},
        current_price=28.0,
    )

    assert synthesis.primary_reason.startswith("A股行为因子 82.4分")
    assert "入选前四" in synthesis.evidence[0]
    assert "近3月因子收益 +6.2%" in synthesis.evidence[1]
    assert "优化权重 0.81" in synthesis.evidence[1]
