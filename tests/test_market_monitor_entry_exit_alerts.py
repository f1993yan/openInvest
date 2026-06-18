import sys
import types
from datetime import datetime

from jobs.market_monitor import (
    _action_score,
    _llm_hold_conflict_adjustment,
    _math_review_position_scale,
    _update_position_exit_plan,
    _review_conclusion,
    _review_score_adjustment,
    apply_repeated_trade_guard,
    build_monitor_window_snapshot,
    call_committee,
    evaluate_entry_exit_triggers,
    is_scheduled_monitor_popup_time,
    select_optimal_actionable_alerts,
    should_send_monitor_summary_popup,
    update_entry_exit_alert_state,
)
from scripts.monitor_window_text import _llm_review_lots_hint


def _result(symbol, price, breakout):
    return {
        "success": True,
        "symbol": symbol,
        "name": symbol,
        "entry_exit_points": {
            "current_price": price,
            "buy_pullback_price": price * 0.96,
            "buy_breakout_price": breakout,
            "stop_loss_price": price * 0.92,
            "take_profit_price": price * 1.16,
            "trim_price": price * 1.12,
            "reentry_price": price * 0.95,
        },
    }


def test_watchlist_breakout_trigger_is_buy():
    triggers = evaluate_entry_exit_triggers(
        10.8,
        {"buy_breakout_price": 10.5, "buy_pullback_price": 9.5},
        is_holding=False,
    )

    assert triggers == [
        {
            "side": "buy",
            "kind": "buy_breakout",
            "level": 10.5,
            "price": 10.8,
        }
    ]


def test_holding_cost_stop_loss_triggers_before_dynamic_stop():
    triggers = evaluate_entry_exit_triggers(
        102.4,
        {
            "buy_pullback_price": 93.35,
            "buy_breakout_price": 104.66,
            "stop_loss_price": 90.55,
        },
        is_holding=True,
        cost=134.01,
    )

    assert triggers == [
        {
            "side": "sell",
            "kind": "cost_stop_loss",
            "level": 117.9288,
            "price": 102.4,
        }
    ]


def test_monitor_summary_popup_schedule_rules():
    assert is_scheduled_monitor_popup_time(datetime(2026, 6, 9, 10, 0))
    assert is_scheduled_monitor_popup_time(datetime(2026, 6, 9, 10, 30))
    assert is_scheduled_monitor_popup_time(datetime(2026, 6, 9, 14, 50))
    assert not is_scheduled_monitor_popup_time(datetime(2026, 6, 9, 10, 10))

    assert not should_send_monitor_summary_popup(
        now=datetime(2026, 6, 9, 10, 10),
        actionable=[],
    )
    assert should_send_monitor_summary_popup(
        now=datetime(2026, 6, 9, 10, 10),
        actionable=[{"symbol": "600900"}],
    )


def test_entry_exit_alert_requires_two_consecutive_triggers(tmp_path):
    state_path = tmp_path / "entry_exit_alert_state.json"

    alerts, rows = update_entry_exit_alert_state(
        results=[_result("600900", 10.0, 10.5)],
        prices={"600900": {"price": 10.0}},
        holding_symbols=set(),
        state_path=state_path,
    )
    assert alerts == []
    assert rows[0]["triggers"] == []

    alerts, rows = update_entry_exit_alert_state(
        results=[_result("600900", 10.7, 11.0)],
        prices={"600900": {"price": 10.7}},
        holding_symbols=set(),
        state_path=state_path,
    )
    assert alerts == []
    assert rows[0]["triggers"][0]["kind"] == "buy_breakout"

    alerts, rows = update_entry_exit_alert_state(
        results=[_result("600900", 11.2, 11.8)],
        prices={"600900": {"price": 11.2}},
        holding_symbols=set(),
        state_path=state_path,
    )
    assert len(alerts) == 1
    assert rows[0]["confirmed"] is True
    assert alerts[0]["matched_sides"] == ["buy"]


def test_entry_exit_alert_confirms_cost_stop_loss_for_holding(tmp_path):
    state_path = tmp_path / "entry_exit_alert_state.json"
    result = {
        "success": True,
        "symbol": "09988",
        "name": "阿里巴巴",
        "entry_exit_points": {
            "current_price": 102.4,
            "buy_pullback_price": 93.35,
            "buy_breakout_price": 104.66,
            "stop_loss_price": 90.55,
        },
    }
    stock = {"symbol": "09988", "position_pct": 3.92, "units": 100, "cost": 134.01}

    alerts, rows = update_entry_exit_alert_state(
        results=[result],
        prices={"09988": {"price": 102.4}},
        holding_symbols={"09988"},
        stocks=[stock],
        state_path=state_path,
    )
    assert len(alerts) == 1
    assert rows[0]["triggers"][0]["kind"] == "position_stop"
    assert rows[0]["confirmed"] is True
    assert alerts[0]["matched_sides"] == ["sell"]

    alerts, rows = update_entry_exit_alert_state(
        results=[result],
        prices={"09988": {"price": 102.4}},
        holding_symbols={"09988"},
        stocks=[stock],
        state_path=state_path,
    )
    assert len(alerts) == 1
    assert rows[0]["confirmed"] is True
    assert alerts[0]["matched_sides"] == ["sell"]


def test_holding_position_exit_plan_does_not_follow_refreshed_committee_levels(tmp_path):
    state_path = tmp_path / "entry_exit_alert_state.json"
    stock = {"symbol": "600900", "position_pct": 5.0, "units": 1000, "cost": 10.0}

    first = _result("600900", 10.0, 10.5)
    first["entry_exit_points"]["atr_pct"] = 2.0
    alerts, rows = update_entry_exit_alert_state(
        results=[first],
        prices={"600900": {"price": 10.0}},
        holding_symbols={"600900"},
        stocks=[stock],
        state_path=state_path,
        now=datetime(2026, 6, 18, 10, 0),
    )
    assert alerts == []
    plan_1 = rows[0]["position_exit_plan"]

    second = _result("600900", 10.2, 11.0)
    second["entry_exit_points"].update({
        "stop_loss_price": 9.8,
        "take_profit_price": 14.0,
        "atr_pct": 3.0,
    })
    alerts, rows = update_entry_exit_alert_state(
        results=[second],
        prices={"600900": {"price": 10.2}},
        holding_symbols={"600900"},
        stocks=[stock],
        state_path=state_path,
        now=datetime(2026, 6, 18, 10, 10),
    )
    plan_2 = rows[0]["position_exit_plan"]

    assert plan_2["hard_stop_price"] == plan_1["hard_stop_price"]
    assert plan_2["effective_stop_price"] == plan_1["effective_stop_price"]
    assert plan_2["take_profit_1_price"] == plan_1["take_profit_1_price"]
    assert rows[0]["entry_exit_points"]["stop_loss_price"] == 9.8


def test_holding_trigger_uses_locked_position_plan_not_new_dynamic_stop(tmp_path):
    state_path = tmp_path / "entry_exit_alert_state.json"
    stock = {"symbol": "600900", "position_pct": 5.0, "units": 1000, "cost": 10.0}
    first = _result("600900", 10.0, 10.5)
    first["entry_exit_points"]["atr_pct"] = 2.0
    update_entry_exit_alert_state(
        results=[first],
        prices={"600900": {"price": 10.0}},
        holding_symbols={"600900"},
        stocks=[stock],
        state_path=state_path,
        now=datetime(2026, 6, 18, 10, 0),
    )

    second = _result("600900", 9.7, 11.0)
    second["entry_exit_points"].update({
        "stop_loss_price": 9.8,
        "take_profit_price": 13.0,
        "atr_pct": 2.0,
    })
    alerts, rows = update_entry_exit_alert_state(
        results=[second],
        prices={"600900": {"price": 9.7}},
        holding_symbols={"600900"},
        stocks=[stock],
        state_path=state_path,
        now=datetime(2026, 6, 18, 10, 10),
    )

    assert alerts == []
    assert rows[0]["triggers"] == []


def test_position_exit_plan_stop_only_moves_up_after_close():
    stock = {"symbol": "600900", "position_pct": 5.0, "units": 1000, "cost": 10.0}
    result = _result("600900", 10.0, 10.5)
    result["entry_exit_points"]["atr_pct"] = 2.0
    first = _update_position_exit_plan(
        None,
        symbol="600900",
        stock=stock,
        result=result,
        current_price=10.0,
        is_holding=True,
        now=datetime(2026, 6, 18, 10, 0),
    )
    assert first is not None
    intraday = _update_position_exit_plan(
        first,
        symbol="600900",
        stock=stock,
        result=result,
        current_price=11.0,
        is_holding=True,
        now=datetime(2026, 6, 18, 14, 0),
    )
    assert intraday["effective_stop_price"] == first["effective_stop_price"]
    after_close = _update_position_exit_plan(
        intraday,
        symbol="600900",
        stock=stock,
        result=result,
        current_price=11.0,
        is_holding=True,
        now=datetime(2026, 6, 18, 15, 1),
    )
    assert after_close["effective_stop_price"] > first["effective_stop_price"]
    lower_close = _update_position_exit_plan(
        after_close,
        symbol="600900",
        stock=stock,
        result=result,
        current_price=10.1,
        is_holding=True,
        now=datetime(2026, 6, 19, 15, 1),
    )
    assert lower_close["effective_stop_price"] == after_close["effective_stop_price"]


def test_monitor_window_snapshot_contains_stable_status_fields():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "长江电力",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.72,
        "suggested_alloc_cny": 3000,
        "fundamental_model": "utility",
        "fundamental_score": 78,
        "quant_view": "trend improving",
        "regime": "REGIME: uptrend",
        "optimizer_review": "CONCLUSION: KEEP\nONE_LINE: 买点质量可以，但需要遵守止损。",
        "entry_exit_points": {
            "model_name": "atr_regime_fallback",
            "current_price": 10.0,
            "buy_pullback_price": 9.5,
            "buy_breakout_price": 10.5,
            "stop_loss_price": 9.0,
            "take_profit_price": 12.0,
            "trim_price": 11.5,
            "reentry_price": 9.4,
            "reward_risk_ratio": 2.0,
            "atr_pct": 2.5,
            "expected_return_pct": 3.0,
        },
    }

    snapshot = build_monitor_window_snapshot(
        round_time="10:30",
        results=[result],
        actionable=[{
            **result,
            "suggested_alloc_cny": 2120,
            "alert_score": 80,
            "optimizer_lots": 2,
            "alert_selected_lots": 2,
            "llm_review_lots": 1,
            "llm_position_scale": "half",
            "llm_position_scale_value": 0.62,
            "llm_position_scale_multiplier": 0.5,
            "llm_risk_components": {"signal_scale": 0.62},
        }],
        prices={"600900": {"price": 10.6, "prev_close": 10.0, "change_pct": 6.0}},
        stocks=[{"symbol": "600900", "name": "长江电力", "sector": "电力", "position_pct": 0, "units": 200}],
        entry_exit_watch=[
            {
                "symbol": "600900",
                "name": "长江电力",
                "entry_exit_points": result["entry_exit_points"],
                "triggers": [{"side": "buy", "kind": "buy_breakout", "level": 10.5, "price": 10.6}],
                "confirmed": True,
            }
        ],
        entry_exit_alerts=[],
        suppressed_alerts=[],
        cash=10000,
        total_assets=100000,
    )

    row = snapshot["rows"][0]
    assert snapshot["counts"]["action_required"] == 1
    assert row["state"] == "action_required"
    assert row["buy_criteria"]["breakout_price"] == 10.5
    assert row["exit_points"]["stop_loss_price"] == 9.0
    assert row["fundamental"]["score"] == 78
    assert row["technical"]["entry_exit_model"] == "atr_regime_fallback"
    assert row["llm_review"]["one_line"].startswith("买点质量")
    assert row["operation"]["suggested_alloc_cny"] == 2120
    assert row["operation"]["optimizer_lots"] == 2
    assert row["operation"]["llm_review_lots"] == 1
    assert row["operation"]["llm_position_scale"] == "half"
    assert _llm_review_lots_hint(row) == "LLM审核推荐1手"
    assert row["units"] == 200
    assert row["is_holding"] is True


class _DummyLedger:
    def __init__(self, trades):
        self._trades = trades

    def list_trades(self, account, limit=100):
        assert account == "committee"
        return self._trades[:limit]


def test_repeated_trade_guard_blocks_recent_buy_without_new_trigger():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "长江电力",
        "verdict": "ACCUMULATE",
        "confidence": 0.7,
        "suggested_alloc_cny": 1800,
        "cio_memo": "memo",
    }
    ledger = _DummyLedger([
        {
            "id": 1,
            "ts": "2099-01-01T00:00:00+00:00",
            "symbol": "600900",
            "direction": "BUY",
            "source": "committee_auto",
            "price": 18.0,
        }
    ])
    state = {
        "symbols": {
            "600900": {
                "entry_exit_points": {
                    "buy_pullback_price": 16.0,
                    "buy_breakout_price": 19.0,
                    "stop_loss_price": 15.0,
                    "take_profit_price": 22.0,
                    "trim_price": 21.0,
                }
            }
        }
    }

    guarded = apply_repeated_trade_guard(
        result,
        stock={"symbol": "600900", "position_pct": 4.0},
        current_price=17.5,
        ledger=ledger,
        entry_exit_state=state,
    )

    assert guarded["execution_blocked"] is True
    assert guarded["verdict"] == "HOLD"
    assert guarded["suggested_alloc_cny"] == 0


def test_repeated_trade_guard_allows_buy_when_previous_breakout_triggers():
    result = {
        "success": True,
        "symbol": "600900",
        "verdict": "ACCUMULATE",
        "confidence": 0.7,
        "suggested_alloc_cny": 1800,
    }
    ledger = _DummyLedger([
        {
            "id": 1,
            "ts": "2099-01-01T00:00:00+00:00",
            "symbol": "600900",
            "direction": "BUY",
            "source": "committee_auto",
            "price": 18.0,
        }
    ])
    state = {
        "symbols": {
            "600900": {
                "entry_exit_points": {
                    "buy_pullback_price": 16.0,
                    "buy_breakout_price": 19.0,
                }
            }
        }
    }

    guarded = apply_repeated_trade_guard(
        result,
        stock={"symbol": "600900", "position_pct": 4.0},
        current_price=19.2,
        ledger=ledger,
        entry_exit_state=state,
    )

    assert guarded is result


def test_alert_optimizer_blocks_limit_up_buy():
    result = {
        "success": True,
        "symbol": "600183",
        "name": "生益科技",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.8,
        "suggested_alloc_cny": 15000,
        "fundamental_score": 80,
        "entry_exit_points": {"reward_risk_ratio": 2.0},
    }

    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"600183": {"price": 150.0, "change_pct": 9.93}},
        cash=100000,
        stocks=[{"symbol": "600183", "position_pct": 0, "min_lot_size": 100}],
        entry_exit_state={},
    )

    assert selected == []
    assert suppressed[0]["reason"] == "limit_up_buy_blocked"


def test_alert_optimizer_uses_cash_constrained_utility():
    high = {
        "success": True,
        "symbol": "600900",
        "name": "高分候选",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.8,
        "suggested_alloc_cny": 3000,
        "fundamental_score": 80,
        "entry_exit_points": {"reward_risk_ratio": 2.0},
    }
    low = {
        "success": True,
        "symbol": "000063",
        "name": "低分候选",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.55,
        "suggested_alloc_cny": 3000,
        "fundamental_score": 30,
        "entry_exit_points": {"reward_risk_ratio": 1.0},
    }

    selected, suppressed = select_optimal_actionable_alerts(
        results=[low, high],
        prices={
            "600900": {"price": 20.0, "change_pct": 1.0},
            "000063": {"price": 20.0, "change_pct": 1.0},
        },
        cash=2500,
        stocks=[
            {"symbol": "600900", "position_pct": 0, "min_lot_size": 100},
            {"symbol": "000063", "position_pct": 0, "min_lot_size": 100},
        ],
        entry_exit_state={},
        max_alerts=4,
    )

    assert [r["symbol"] for r in selected] == ["600900"]
    assert selected[0]["suggested_alloc_cny"] == 2000
    assert any(item["symbol"] == "000063" for item in suppressed)


def test_review_conclusion_uses_structured_field_not_stray_words():
    assert _review_conclusion({"optimizer_review": "CONCLUSION: caution\nONE_LINE: 需要等回踩"}) == "caution"
    assert _review_conclusion({"optimizer_review": "CONCLUSION: REJECT\nONE_LINE: 入场风险太高"}) == "reject"
    assert _review_conclusion({"optimizer_review": "CONCLUSION: approve\nONE_LINE: 风险收益可接受"}) == "approve"
    assert _review_conclusion({"optimizer_review": "NOTE: this says not reject, but has no structured conclusion"}) == ""


def test_review_score_adjustment_is_weak_bayesian_evidence():
    caution = _review_score_adjustment({"optimizer_review": "CONCLUSION: caution"}, 70.0)
    reject = _review_score_adjustment({"optimizer_review": "CONCLUSION: reject"}, 70.0)
    approve = _review_score_adjustment({"optimizer_review": "CONCLUSION: approve"}, 70.0)

    assert reject < caution < 0 < approve
    assert abs(approve) < abs(caution) < abs(reject)
    assert reject > -10.0


def test_reject_review_can_drop_marginal_candidate_but_caution_does_not():
    base = {
        "success": True,
        "symbol": "600900",
        "name": "边际候选",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.59,
        "suggested_alloc_cny": 2000,
        "fundamental_score": 75,
        "entry_exit_points": {"reward_risk_ratio": 2.0},
    }
    stock = {"symbol": "600900", "position_pct": 0, "min_lot_size": 100}
    price = {"price": 20.0, "change_pct": 1.0}

    caution_score = _action_score(
        {**base, "optimizer_review": "CONCLUSION: caution"},
        stock=stock,
        price_info=price,
        triggers=[],
    )
    reject_score = _action_score(
        {**base, "optimizer_review": "CONCLUSION: reject"},
        stock=stock,
        price_info=price,
        triggers=[],
    )

    assert caution_score >= 55.0
    assert reject_score < 55.0


def test_llm_hold_conflict_is_soft_evidence_not_hard_block():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "强信号候选",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.93,
        "suggested_alloc_cny": 1000,
        "fundamental_score": 80,
        "cio_memo": "LLM=HOLD -> ACCUMULATE because optimizer evidence is stronger",
        "entry_exit_points": {"reward_risk_ratio": 2.0},
    }

    assert _llm_hold_conflict_adjustment(result, 90.0, has_buy_trigger=False) < 0

    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"600900": {"price": 10.0, "change_pct": 1.0}},
        cash=2000,
        stocks=[{"symbol": "600900", "position_pct": 0, "min_lot_size": 100}],
        entry_exit_state={},
    )

    assert [r["symbol"] for r in selected] == ["600900"]
    assert all(item.get("reason") != "llm_hold_without_buy_trigger" for item in suppressed)


def test_math_review_position_scale_reduces_lots_from_objective_risk():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "风险复核候选",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.95,
        "suggested_alloc_cny": 10000,
        "fundamental_score": 80,
        "optimizer_review": "CONCLUSION: caution\nRISK_FLAGS: 条件CVaR 95损失21.92%偏高",
        "entry_exit_points": {
            "reward_risk_ratio": 2.0,
            "stop_loss_price": 8.8,
            "cvar_95_loss_pct": 21.92,
        },
    }

    review = _math_review_position_scale(
        result,
        stock={"symbol": "600900", "position_pct": 0, "min_lot_size": 100},
        price=10.0,
        lots=10,
        lot_size=100,
        portfolio_value=100000,
        alert_score=85.0,
        max_single_position_pct=25,
    )

    assert review["position_scale"] == "half"
    assert review["recommended_lots"] == 5
    assert review["risk_components"]["cvar_95_loss_pct"] == 21.92


def test_alert_optimizer_keeps_optimizer_lots_and_llm_review_lots():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "风险复核候选",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.95,
        "suggested_alloc_cny": 10000,
        "fundamental_score": 80,
        "optimizer_review": "CONCLUSION: caution\nRISK_FLAGS: 条件CVaR 95损失21.92%偏高",
        "entry_exit_points": {
            "reward_risk_ratio": 2.0,
            "stop_loss_price": 8.8,
            "cvar_95_loss_pct": 21.92,
        },
    }

    selected, _ = select_optimal_actionable_alerts(
        results=[result],
        prices={"600900": {"price": 10.0, "change_pct": 1.0}},
        cash=10000,
        stocks=[{"symbol": "600900", "position_pct": 0, "min_lot_size": 100}],
        entry_exit_state={},
        portfolio_value=100000,
        max_alerts=1,
    )

    assert selected
    row = selected[0]
    assert row["optimizer_lots"] == row["alert_selected_lots"]
    assert row["llm_review_lots"] < row["optimizer_lots"]
    assert row["llm_position_scale"] in {"tiny", "half"}


def test_alert_optimizer_prefers_risk_distributed_basket():
    ai_1 = {
        "success": True,
        "symbol": "600900",
        "name": "AI高分1",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.86,
        "suggested_alloc_cny": 1000,
        "fundamental_score": 78,
        "entry_exit_points": {"reward_risk_ratio": 2.0, "stop_loss_price": 9.2},
    }
    ai_2 = {
        "success": True,
        "symbol": "600901",
        "name": "AI高分2",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.84,
        "suggested_alloc_cny": 1000,
        "fundamental_score": 76,
        "entry_exit_points": {"reward_risk_ratio": 2.0, "stop_loss_price": 9.2},
    }
    power = {
        "success": True,
        "symbol": "000063",
        "name": "电力分散",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.80,
        "suggested_alloc_cny": 1000,
        "fundamental_score": 72,
        "entry_exit_points": {"reward_risk_ratio": 2.0, "stop_loss_price": 9.2},
    }

    selected, _ = select_optimal_actionable_alerts(
        results=[ai_1, ai_2, power],
        prices={
            "600900": {"price": 10.0, "change_pct": 1.0},
            "600901": {"price": 10.0, "change_pct": 1.0},
            "000063": {"price": 10.0, "change_pct": 1.0},
        },
        cash=3000,
        stocks=[
            {"symbol": "600900", "sector": "AI", "position_pct": 0, "min_lot_size": 100},
            {"symbol": "600901", "sector": "AI", "position_pct": 0, "min_lot_size": 100},
            {"symbol": "000063", "sector": "电力", "position_pct": 0, "min_lot_size": 100},
        ],
        entry_exit_state={},
        max_alerts=2,
        portfolio_value=10000,
        max_sector_position_pct=12,
    )

    assert len(selected) == 2
    assert {r["alert_sector"] for r in selected} == {"AI", "电力"}
    assert any(r["symbol"] == "000063" for r in selected)


def test_call_committee_uses_direct_backend_call(monkeypatch):
    captured = {}

    class _FakeHolding:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _FakeRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _FakeResponse:
        def model_dump(self):
            return {
                "success": True,
                "symbol": "600900",
                "verdict": "HOLD",
            }

    def _fake_direct(req):
        captured["req"] = req
        return _FakeResponse()

    def _fail_http(*args, **kwargs):
        raise AssertionError("call_committee should not use HTTP")

    fake_backend = types.ModuleType("backend.server")
    fake_backend.CommitteeRequest = _FakeRequest
    fake_backend.Holding = _FakeHolding
    fake_backend.run_committee_direct = _fake_direct
    monkeypatch.setitem(sys.modules, "backend.server", fake_backend)
    monkeypatch.setattr("jobs.market_monitor.requests.post", _fail_http)

    result = call_committee(
        symbol="600900",
        name="test",
        market="a",
        position_pct=5.0,
        cost=10.0,
        current_price=11.0,
        total_assets=100000.0,
        cash=10000.0,
        all_holdings=[{"symbol": "600900", "name": "test", "position_pct": 5.0, "cost": 10.0}],
    )

    assert result == {"success": True, "symbol": "600900", "verdict": "HOLD"}
    assert captured["req"].symbol == "600900"
    assert captured["req"].holdings[0].weight_pct == 5.0
