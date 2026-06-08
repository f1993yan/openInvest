from types import SimpleNamespace

from core.entry_exit_points import compute_entry_exit_points


def _metrics(**overrides):
    base = {
        "current_price": 10.0,
        "atr_pct": 2.0,
        "return_30d": 0.05,
    }
    base.update(overrides)
    return base


def test_entry_exit_points_use_conditional_quantiles_and_cvar():
    stats = SimpleNamespace(
        low_confidence=False,
        mean_return_pct=5.0,
        q20_return_pct=-3.0,
        q80_return_pct=8.0,
        cvar_95_loss_pct=7.0,
    )

    plan = compute_entry_exit_points(
        symbol="600900",
        current_price=10.0,
        metrics=_metrics(atr_pct=1.0),
        regime_brief="REGIME: uptrend",
        conditional_return_stats=stats,
        expected_return_pct=5.5,
    )

    assert plan.model_name == "regime_quantile_atr_cvar"
    assert plan.low_confidence is False
    assert plan.buy_pullback_price < plan.current_price
    assert plan.buy_breakout_price > plan.current_price
    assert plan.stop_loss_price < plan.current_price
    assert plan.take_profit_price > plan.current_price
    assert plan.reward_risk_ratio >= 1.5
    assert "stop_loss_price:" in plan.audit_text()


def test_entry_exit_points_fallback_when_distribution_missing():
    plan = compute_entry_exit_points(
        symbol="688099",
        current_price=20.0,
        metrics=_metrics(current_price=20.0, atr_pct=2.5),
        regime_brief="REGIME: range_bound",
    )

    assert plan.model_name == "atr_regime_fallback"
    assert plan.low_confidence is True
    assert plan.stop_loss_price < 20.0
    assert plan.take_profit_price > 20.0
    assert plan.reentry_price < 20.0


def test_entry_exit_points_missing_price_is_unavailable():
    plan = compute_entry_exit_points(
        symbol="600900",
        current_price=None,
        metrics={},
    )

    assert plan.model_name == "unavailable"
    assert plan.current_price == 0.0
    assert plan.reason == "missing_current_price"
