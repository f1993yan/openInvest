from core.decision_optimizer import optimize_committee_decision
from core.fundamental_model import assess_fundamentals
from types import SimpleNamespace


def _metrics(**overrides):
    base = {
        "current_price": 10.0,
        "ma20": 11.0,
        "ma120": 10.0,
        "atr_pct": 2.0,
        "volatility_annualized": 0.20,
        "return_30d": 0.08,
        "price_quantile_2y": 0.50,
        "rsi14": 55.0,
    }
    base.update(overrides)
    return base


def test_optimizer_holds_when_cash_cannot_buy_one_lot():
    decision = optimize_committee_decision(
        parsed={"verdict": "BUY", "confidence": 0.9, "alloc_cny": 500},
        metrics=_metrics(),
        symbol="600900",
        regime_brief="REGIME: uptrend",
        current_price=10.0,
        total_assets=100_000,
        available_cash=999,
        position_pct=0.0,
        min_lot_size=100,
    )

    assert decision.verdict == "HOLD"
    assert decision.alloc_cny == 0
    assert decision.lots == 0


def test_optimizer_outputs_integer_lot_buy_when_edge_is_positive():
    decision = optimize_committee_decision(
        parsed={"verdict": "BUY", "confidence": 0.9, "alloc_cny": 10_000},
        metrics=_metrics(
            atr_pct=1.0,
            volatility_annualized=0.08,
            return_30d=0.20,
            price_quantile_2y=0.30,
        ),
        symbol="600900",
        regime_brief="REGIME: uptrend",
        current_price=10.0,
        total_assets=100_000,
        available_cash=10_000,
        position_pct=0.0,
        min_lot_size=100,
    )

    assert decision.verdict in {"BUY", "ACCUMULATE"}
    assert decision.alloc_cny > 0
    assert decision.alloc_cny % 1000 == 0
    assert decision.lots == decision.alloc_cny // 1000


def test_black_litterman_anchor_can_pull_toward_target_weight():
    decision = optimize_committee_decision(
        parsed={"verdict": "HOLD", "confidence": 0.5, "alloc_cny": 0},
        metrics=_metrics(
            atr_pct=1.0,
            volatility_annualized=0.08,
            return_30d=0.0,
            price_quantile_2y=0.50,
        ),
        symbol="600900",
        regime_brief="REGIME: range_bound",
        current_price=10.0,
        total_assets=100_000,
        available_cash=30_000,
        position_pct=0.0,
        target_position_pct=20.0,
        min_lot_size=100,
    )

    assert decision.verdict in {"BUY", "ACCUMULATE"}
    assert decision.alloc_cny > 0
    assert decision.target_position_pct == 20.0


def test_optimizer_uses_conditional_return_distribution_stats():
    stats = SimpleNamespace(
        low_confidence=False,
        mean_return_pct=6.5,
        p_up=0.62,
        p_down=0.18,
        p_flat=0.20,
        upside_mean_pct=9.0,
        downside_mean_pct=-4.0,
        q20_return_pct=-2.0,
        q80_return_pct=7.5,
        cvar_95_loss_pct=6.0,
    )

    decision = optimize_committee_decision(
        parsed={"verdict": "HOLD", "confidence": 0.5, "alloc_cny": 0},
        metrics=_metrics(
            atr_pct=1.0,
            volatility_annualized=0.08,
            return_30d=-0.20,
            price_quantile_2y=0.90,
        ),
        symbol="600900",
        regime_brief="REGIME: uptrend",
        current_price=10.0,
        total_assets=100_000,
        available_cash=20_000,
        position_pct=0.0,
        target_position_pct=0.0,
        min_lot_size=100,
        conditional_return_stats=stats,
    )

    assert decision.expected_return_pct == 6.5
    assert decision.verdict in {"BUY", "ACCUMULATE"}
    assert decision.alloc_cny > 0
    assert decision.cvar_95_loss_pct == 6.0
    assert "conditional_cvar_95_loss=6.00%" in decision.audit_text()


def test_fundamental_assessment_soft_adjusts_black_litterman_anchor():
    assessment = assess_fundamentals(
        symbol="688099",
        name="晶晨股份",
        sector="半导体",
        industry="SoC芯片",
        metrics={
            "revenue_growth": 0.28,
            "gross_margin": 0.48,
            "operating_margin": 0.18,
            "rd_to_revenue": 0.15,
            "roic": 0.18,
            "fcf_yield": 0.04,
            "ps": 5.0,
            "peg": 1.2,
            "net_debt_to_ebitda": -0.2,
        },
    )

    decision = optimize_committee_decision(
        parsed={"verdict": "HOLD", "confidence": 0.5, "alloc_cny": 0},
        metrics=_metrics(
            atr_pct=1.0,
            volatility_annualized=0.08,
            return_30d=0.0,
            price_quantile_2y=0.50,
        ),
        symbol="688099",
        regime_brief="REGIME: range_bound",
        current_price=10.0,
        total_assets=100_000,
        available_cash=30_000,
        position_pct=0.0,
        target_position_pct=20.0,
        min_lot_size=100,
        fundamental_assessment=assessment,
    )

    assert decision.target_position_pct > 20.0
    assert decision.fundamental_model == "growth_innovation"
    assert decision.fundamental_anchor_multiplier > 1.0
    assert "fundamental_model=growth_innovation" in decision.audit_text()


def test_exit_policy_sell_evidence_is_ignored_for_existing_position():
    policy = SimpleNamespace(
        sell_utility_adjustment_pct=8.0,
        sell_reliability=0.80,
        sell_evidence_score=0.65,
    )

    common = dict(
        parsed={"verdict": "HOLD", "confidence": 0.55, "alloc_cny": 0},
        metrics=_metrics(
            atr_pct=2.0,
            volatility_annualized=0.16,
            return_30d=0.02,
            price_quantile_2y=0.95,
            rsi14=72.0,
        ),
        symbol="600900",
        regime_brief="REGIME: range_bound",
        current_price=10.0,
        total_assets=100_000,
        available_cash=10_000,
        position_pct=25.0,
        target_position_pct=25.0,
        min_lot_size=100,
    )
    baseline = optimize_committee_decision(**common)
    decision = optimize_committee_decision(**common, position_exit_policy=policy)

    assert decision.verdict == baseline.verdict
    assert decision.alloc_cny == baseline.alloc_cny
    assert decision.expected_return_pct == baseline.expected_return_pct
    assert decision.exit_policy_adjustment_pct == 0.0


def test_exit_policy_sell_evidence_is_ignored_without_position():
    policy = SimpleNamespace(
        sell_utility_adjustment_pct=8.0,
        sell_reliability=0.80,
        sell_evidence_score=0.65,
    )

    decision = optimize_committee_decision(
        parsed={"verdict": "HOLD", "confidence": 0.55, "alloc_cny": 0},
        metrics=_metrics(price_quantile_2y=0.95, rsi14=72.0),
        symbol="600900",
        regime_brief="REGIME: range_bound",
        current_price=10.0,
        total_assets=100_000,
        available_cash=10_000,
        position_pct=0.0,
        target_position_pct=0.0,
        min_lot_size=100,
        position_exit_policy=policy,
    )

    assert decision.exit_policy_adjustment_pct == 0.0


def test_a_share_behavioral_factor_replaces_old_technical_expected_return():
    factor = SimpleNamespace(
        low_confidence=False,
        expected_return_pct=7.25,
        target_weight_pct=30.0,
        score=88.0,
        selected=True,
        model_key="a_share_behavioral_v1",
        sample_size=40,
        optimizer_weight=0.91,
    )
    decision = optimize_committee_decision(
        parsed={"verdict": "SELL", "confidence": 0.9, "alloc_cny": -10_000},
        metrics=_metrics(return_30d=-0.50, rsi14=80, price_quantile_2y=0.95),
        symbol="600900",
        regime_brief="REGIME: crash",
        current_price=10.0,
        total_assets=100_000,
        available_cash=40_000,
        position_pct=0.0,
        min_lot_size=100,
        market="a",
        behavioral_assessment=factor,
    )

    assert decision.expected_return_pct == 7.25
    assert decision.target_position_pct == 30.0
    assert decision.behavioral_selected is True
    assert abs(decision.behavioral_optimizer_weight - 0.91) < 1e-9
    assert decision.verdict in {"BUY", "ACCUMULATE"}


def test_non_a_share_keeps_existing_baseline_when_factor_is_supplied():
    factor = SimpleNamespace(
        low_confidence=False,
        expected_return_pct=12.0,
        target_weight_pct=35.0,
        score=99.0,
        selected=True,
        model_key="a_share_behavioral_v1",
        sample_size=40,
    )
    common = dict(
        parsed={"verdict": "HOLD", "confidence": 0.5, "alloc_cny": 0},
        metrics=_metrics(return_30d=0.02),
        symbol="00700",
        regime_brief="REGIME: range_bound",
        current_price=500.0,
        total_assets=200_000,
        available_cash=100_000,
        position_pct=0.0,
        min_lot_size=100,
        market="hk",
    )
    baseline = optimize_committee_decision(**common)
    with_factor = optimize_committee_decision(**common, behavioral_assessment=factor)

    assert with_factor.expected_return_pct == baseline.expected_return_pct
    assert with_factor.target_position_pct == baseline.target_position_pct


def test_production_a_share_fails_closed_without_valid_behavioral_factor():
    common = dict(
        parsed={"verdict": "BUY", "confidence": 0.9, "alloc_cny": 20_000},
        metrics=_metrics(return_30d=0.20),
        symbol="600900",
        regime_brief="REGIME: uptrend",
        current_price=10.0,
        total_assets=100_000,
        available_cash=50_000,
        position_pct=12.0,
        min_lot_size=100,
        market="a",
        require_a_share_behavioral=True,
    )

    missing = optimize_committee_decision(**common)
    low_confidence = optimize_committee_decision(
        **common,
        behavioral_assessment=SimpleNamespace(
            low_confidence=True,
            score=70.0,
            target_weight_pct=20.0,
            selected=True,
            model_key="a_share_behavioral_v1",
        ),
    )

    assert missing.verdict == "WAIT"
    assert missing.alloc_cny == 0
    assert missing.target_position_pct == 12.0
    assert missing.reason == "a_share_behavioral_factor_unavailable"
    assert low_confidence.verdict == "WAIT"
    assert low_confidence.alloc_cny == 0
    assert low_confidence.reason == "a_share_behavioral_factor_unavailable"


def test_behavioral_no_trade_band_ignores_retired_exit_policy():
    factor = SimpleNamespace(
        low_confidence=False,
        expected_return_pct=-4.0,
        target_weight_pct=20.0,
        score=70.0,
        selected=True,
        model_key="a_share_behavioral_v1",
        sample_size=40,
    )
    common = dict(
        parsed={"verdict": "SELL", "confidence": 0.8, "alloc_cny": -5_000},
        metrics=_metrics(return_30d=-0.10),
        symbol="600900",
        regime_brief="REGIME: downtrend",
        current_price=10.0,
        total_assets=100_000,
        available_cash=10_000,
        position_pct=22.0,
        min_lot_size=100,
        market="a",
        behavioral_assessment=factor,
    )
    held = optimize_committee_decision(**common)
    risk_sell = optimize_committee_decision(
        **common,
        position_exit_policy=SimpleNamespace(
            sell_utility_adjustment_pct=4.0,
            sell_reliability=0.8,
            sell_evidence_score=0.7,
        ),
    )

    assert held.verdict == "HOLD"
    assert risk_sell.verdict == held.verdict
    assert risk_sell.alloc_cny == held.alloc_cny
    assert risk_sell.exit_policy_adjustment_pct == 0.0
