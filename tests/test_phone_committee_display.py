from utils.phone_committee import build_mobile_recommendation_text


def test_mobile_recommendation_displays_behavioral_factor_parameters():
    text = build_mobile_recommendation_text(
        symbol="600900",
        name="测试标的",
        market="a",
        current_price=28.0,
        change_pct=1.2,
        verdict="ACCUMULATE",
        confidence=0.78,
        suggested_alloc_cny=5000,
        is_holding=False,
        entry_exit_points={
            "buy_pullback_price": 27.2,
            "buy_breakout_price": 28.8,
            "stop_loss_price": 25.9,
            "take_profit_price": 32.1,
        },
        position_exit_policy={},
        right_side_trend_gate={"allow": True, "reason": "trend_confirmed"},
        fundamental_score=72.0,
        regime_brief="REGIME: uptrend",
        cio_memo="",
        behavioral_factor={
            "score": 82.4,
            "target_weight_pct": 24.6,
            "optimizer_weight": 0.81,
            "trailing_3m_factor_return_pct": 6.2,
            "trailing_3m_hit_rate": 0.57,
            "trailing_3m_sample_size": 63,
            "selected": True,
            "low_confidence": False,
        },
        decision_mode="algorithm_only",
    )

    assert "决策来源: 算法直出，本轮未调用LLM委员会或LLM审核。" in text
    assert "A股行为因子: 82.4分，入选前四，目标仓位 24.6%" in text
    assert "近3月 +6.2%，命中 57% (n=63)，优化权重 0.81" in text
    assert "回调 27.20" in text
    assert "止损 25.90" in text
