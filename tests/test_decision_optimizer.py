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


def test_exit_policy_sell_evidence_can_shift_existing_position_to_trim():
    policy = SimpleNamespace(
        sell_utility_adjustment_pct=8.0,
        sell_reliability=0.80,
        sell_evidence_score=0.65,
    )

    decision = optimize_committee_decision(
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
        position_exit_policy=policy,
    )

    assert decision.verdict in {"TRIM", "SELL"}
    assert decision.alloc_cny < 0
    assert decision.exit_policy_adjustment_pct > 0
    assert "exit_policy_adjustment_30d=" in decision.audit_text()


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
