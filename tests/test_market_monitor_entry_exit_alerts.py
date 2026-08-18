import json
import pytest
import sys
import types
from datetime import datetime

from jobs.market_monitor import (
    _action_score,
    apply_sector_cache_to_stocks,
    _llm_hold_conflict_adjustment,
    _math_review_position_scale,
    _sell_committee_execution_edge,
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
from jobs.market_monitor_notify import send_action_required_email
from jobs.trading_mode import normalize_trading_mode
from scripts.monitor_window_text import _llm_review_lots_hint
from scripts.monitor_window_text import _operation_summary


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


def test_holding_cost_and_exit_lines_do_not_create_sell_triggers():
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

    assert triggers == []


def test_monitor_summary_popup_schedule_rules():
    assert is_scheduled_monitor_popup_time(datetime(2026, 6, 9, 10, 0))
    assert is_scheduled_monitor_popup_time(datetime(2026, 6, 9, 10, 30))
    assert is_scheduled_monitor_popup_time(datetime(2026, 6, 9, 15, 0))
    assert not is_scheduled_monitor_popup_time(datetime(2026, 6, 9, 10, 10))

    assert not should_send_monitor_summary_popup(
        now=datetime(2026, 6, 9, 10, 10),
        actionable=[],
    )
    assert should_send_monitor_summary_popup(
        now=datetime(2026, 6, 9, 10, 10),
        actionable=[{"symbol": "600900"}],
    )


def test_action_required_email_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("INVEST_MONITOR_EMAIL_ACTIONS", "0")
    import jobs.market_monitor_notify as notify

    monkeypatch.setattr(notify, "ACTION_EMAIL_STATE_PATH", tmp_path / "action_email_state.json")
    called = {"value": False}
    monkeypatch.setattr("services.notifier.send_email_html", lambda **_: called.update(value=True) or "x@example.com")

    receiver = send_action_required_email(
        round_time="10:30",
        actionable=[{"symbol": "600900", "name": "长江电力", "verdict": "BUY", "suggested_alloc_cny": 5000}],
        prices={"600900": {"price": 25.0}},
        stocks=[{"symbol": "600900", "min_lot_size": 100}],
        now=datetime(2026, 7, 1, 10, 30),
    )

    assert receiver == ""
    assert called["value"] is False


def test_action_required_email_renders_lots_reason_and_dedupes(monkeypatch, tmp_path):
    monkeypatch.setenv("INVEST_MONITOR_EMAIL_ACTIONS", "1")
    import jobs.market_monitor_notify as notify

    monkeypatch.setattr(notify, "ACTION_EMAIL_STATE_PATH", tmp_path / "action_email_state.json")
    sent = []
    monkeypatch.setattr("services.notifier.render_markdown_email", lambda md, **_: f"<html>{md}</html>")

    def _send_email(**kwargs):
        sent.append(kwargs)
        return "me@example.com"

    monkeypatch.setattr("services.notifier.send_email_html", _send_email)
    action = {
        "symbol": "600900",
        "name": "长江电力",
        "verdict": "ACCUMULATE",
        "confidence": 0.8,
        "suggested_alloc_cny": 5000,
        "optimizer_lots": 2,
        "llm_review_lots": 1,
        "alert_source": "selected_by_cash_risk_optimizer",
        "alert_triggers": [{"side": "buy", "kind": "buy_breakout", "level": 26.5}],
        "optimizer_review": "CONCLUSION: approve\nONE_LINE: 突破买点触发，风险收益比可接受。",
    }

    first = send_action_required_email(
        round_time="10:30",
        actionable=[action],
        prices={"600900": {"price": 26.6}},
        stocks=[{"symbol": "600900", "min_lot_size": 100}],
        now=datetime(2026, 7, 1, 10, 30),
    )
    second = send_action_required_email(
        round_time="10:40",
        actionable=[action],
        prices={"600900": {"price": 26.8}},
        stocks=[{"symbol": "600900", "min_lot_size": 100}],
        now=datetime(2026, 7, 1, 10, 40),
    )

    assert first == "me@example.com"
    assert second == ""
    assert len(sent) == 1
    plain = sent[0]["plain_body"]
    assert "长江电力 (600900)" in plain
    assert "2手（LLM审核推荐1手）" in plain
    assert "buy_breakout@26.50" in plain
    assert "突破买点触发" in plain


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


@pytest.mark.skip(reason="Position exit discipline is disabled")
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


@pytest.mark.skip(reason="Position exit discipline is disabled")
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


def test_retired_position_exit_plan_compatibility_stub_returns_none():
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
    assert first is None


def test_monitor_window_snapshot_contains_stable_status_fields():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "长江电力",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.72,
        "suggested_alloc_cny": 3000,
        "analysis_id": "monitor:600900:test",
        "decision_id": "real-decision-600900-test",
        "shadow_result": {
            "success": True,
            "symbol": "600900",
            "verdict": "SELL",
            "suggested_alloc_cny": -5000,
            "decision_id": "private-shadow-decision",
        },
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
        trading_mode="risk_off",
    )

    row = snapshot["rows"][0]
    assert snapshot["trading_mode"] == {"mode": "cash_recovery", "label": "现金回收"}
    assert snapshot["monitor_action_email_enabled"] is True
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
    assert row["analysis_id"] == "monitor:600900:test"
    assert row["decision_id"] == "real-decision-600900-test"
    assert row["operation"]["decision_id"] == "real-decision-600900-test"
    assert "shadow_result" not in json.dumps(snapshot, ensure_ascii=False)
    assert "private-shadow-decision" not in json.dumps(snapshot, ensure_ascii=False)
    assert _llm_review_lots_hint(row) == "LLM审核推荐1手"
    assert row["units"] == 200
    assert row["is_holding"] is True


def test_monitor_window_marks_unrepairable_behavioral_factor_as_unavailable():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "长江电力",
        "market": "a",
        "verdict": "WAIT",
        "confidence": 0.35,
        "suggested_alloc_cny": 0,
        "behavioral_factor": {
            "low_confidence": True,
            "reason": "behavioral_cross_section_or_history_unavailable",
        },
    }
    snapshot = build_monitor_window_snapshot(
        round_time="10:10",
        results=[result],
        actionable=[],
        prices={"600900": {"price": 28.0, "change_pct": 1.0}},
        stocks=[{"symbol": "600900", "name": "长江电力", "market": "a", "units": 500}],
        entry_exit_watch=[],
        entry_exit_alerts=[],
        suppressed_alerts=[],
        cash=10000,
        total_assets=100000,
    )

    row = snapshot["rows"][0]
    assert row["state"] == "factor_unavailable"
    assert row["operation"]["status"] == "factor_unavailable"
    assert row["operation"]["reason"] == "behavioral_cross_section_or_history_unavailable"
    assert row["behavioral_factor"]["low_confidence"] is True


def test_monitor_window_marks_unavailable_hk_spatio_factor_without_a_share_label():
    result = {
        "success": True,
        "symbol": "00700",
        "name": "腾讯控股",
        "market": "hk",
        "verdict": "WAIT",
        "confidence": 0.35,
        "suggested_alloc_cny": 0,
        "hk_spatio_factor": {
            "low_confidence": True,
            "reason": "hk_spatio_cross_section_or_history_unavailable",
        },
    }
    snapshot = build_monitor_window_snapshot(
        round_time="10:10",
        results=[result],
        actionable=[],
        prices={"00700": {"price": 500.0, "change_pct": 1.0}},
        stocks=[{"symbol": "00700", "name": "腾讯控股", "market": "hk", "units": 100}],
        entry_exit_watch=[],
        entry_exit_alerts=[],
        suppressed_alerts=[],
        cash=10000,
        total_assets=100000,
    )

    row = snapshot["rows"][0]
    assert row["state"] == "factor_unavailable"
    assert row["operation"]["reason"] == "hk_spatio_cross_section_or_history_unavailable"
    assert row["hk_spatio_factor"]["low_confidence"] is True
    assert row["behavioral_factor"] == {}


def test_monitor_window_snapshot_fills_sector_from_cache_when_config_is_empty(tmp_path, monkeypatch):
    import jobs.market_monitor_snapshot as snapshot_mod

    cache_path = tmp_path / "sector_cache.json"
    cache_path.write_text(
        json.dumps({"mapping": {"600900": "电力行业"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(snapshot_mod, "SECTOR_CACHE_PATH", cache_path)

    snapshot = build_monitor_window_snapshot(
        round_time="10:30",
        results=[{"success": True, "symbol": "600900", "name": "长江电力", "market": "a", "verdict": "HOLD"}],
        actionable=[],
        prices={"600900": {"price": 10.6, "prev_close": 10.0, "change_pct": 0.2}},
        stocks=[{"symbol": "600900", "name": "长江电力", "market": "a"}],
        entry_exit_watch=[],
        entry_exit_alerts=[],
        suppressed_alerts=[],
        cash=10000,
        total_assets=100000,
    )

    assert snapshot["rows"][0]["sector"] == "电力行业"
    assert snapshot["rows"][0]["industry"] == "电力行业"


def test_apply_sector_cache_matches_symbol_variants():
    rows = apply_sector_cache_to_stocks(
        [{"symbol": "SH600900", "market": "a", "sector": ""}],
        {"600900": "电力行业"},
    )

    assert rows[0]["sector"] == "电力行业"
    assert rows[0]["sector_source"] == "eastmoney_sector_cache"


def test_monitor_window_snapshot_can_disable_action_email_flag():
    snapshot = build_monitor_window_snapshot(
        round_time="10:30",
        results=[],
        actionable=[],
        prices={},
        stocks=[],
        entry_exit_watch=[],
        entry_exit_alerts=[],
        suppressed_alerts=[],
        cash=10000,
        total_assets=100000,
        monitor_action_email_enabled=False,
    )

    assert snapshot["monitor_action_email_enabled"] is False


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


def test_repeated_trade_guard_allows_sell_without_retired_exit_trigger_after_buy():
    result = {
        "success": True,
        "symbol": "002463",
        "name": "沪电股份",
        "market": "a",
        "verdict": "SELL",
        "confidence": 0.82,
        "suggested_alloc_cny": -18000,
    }
    ledger = _DummyLedger([
        {
            "id": 2,
            "ts": "2099-01-01T00:00:00+00:00",
            "trade_date": "2099-01-01",
            "symbol": "002463",
            "direction": "BUY",
            "source": "committee_auto",
            "price": 146.0,
        }
    ])
    state = {
        "symbols": {
            "002463": {
                "position_exit_plan": {
                    "effective_stop_price": 138.0,
                    "take_profit_1_price": 158.0,
                    "take_profit_2_price": 168.0,
                }
            }
        }
    }

    guarded = apply_repeated_trade_guard(
        result,
        stock={"symbol": "002463", "market": "a", "position_pct": 10.0, "units": 200, "cost": 146.0},
        current_price=145.0,
        ledger=ledger,
        entry_exit_state=state,
    )

    assert guarded is result


def test_repeated_trade_guard_ignores_stale_exit_trigger_after_buy():
    result = {
        "success": True,
        "symbol": "002463",
        "name": "沪电股份",
        "market": "a",
        "verdict": "SELL",
        "confidence": 0.82,
        "suggested_alloc_cny": -18000,
    }
    ledger = _DummyLedger([
        {
            "id": 2,
            "ts": "2099-01-01T00:00:00+00:00",
            "trade_date": "2099-01-01",
            "symbol": "002463",
            "direction": "BUY",
            "source": "committee_auto",
            "price": 146.0,
        }
    ])
    state = {
        "symbols": {
            "002463": {
                "position_exit_plan": {
                    "effective_stop_price": 138.0,
                    "take_profit_1_price": 158.0,
                    "take_profit_2_price": 168.0,
                }
            }
        }
    }

    guarded = apply_repeated_trade_guard(
        result,
        stock={"symbol": "002463", "market": "a", "position_pct": 10.0, "units": 200, "cost": 146.0},
        current_price=137.5,
        ledger=ledger,
        entry_exit_state=state,
    )

    assert guarded is result


def test_repeated_trade_guard_still_blocks_recent_same_direction_sell():
    result = {
        "success": True,
        "symbol": "002463",
        "market": "a",
        "verdict": "SELL",
        "confidence": 0.82,
        "suggested_alloc_cny": -18000,
    }
    ledger = _DummyLedger([
        {
            "id": 3,
            "ts": "2099-01-01T00:00:00+00:00",
            "trade_date": "2099-01-01",
            "symbol": "002463",
            "direction": "SELL",
            "source": "committee_auto",
            "price": 145.0,
        }
    ])

    guarded = apply_repeated_trade_guard(
        result,
        stock={"symbol": "002463", "market": "a", "position_pct": 10.0, "units": 200},
        current_price=144.0,
        ledger=ledger,
        entry_exit_state={},
    )

    assert guarded["execution_blocked"] is True
    assert guarded["execution_block_reason"] == "recent_same_direction_committee_trade_without_new_entry_exit_trigger"
    assert guarded["verdict"] == "HOLD"


def test_alert_selector_allows_committee_sell_after_recent_real_buy():
    result = {
        "success": True,
        "symbol": "002463",
        "name": "沪电股份",
        "market": "a",
        "verdict": "SELL",
        "confidence": 0.82,
        "suggested_alloc_cny": -18000,
        "fundamental_score": 60,
    }
    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"002463": {"price": 145.0, "change_pct": -1.0}},
        cash=10000,
        stocks=[{"symbol": "002463", "market": "a", "position_pct": 10.0, "units": 200, "min_lot_size": 100}],
        entry_exit_state={},
        portfolio_value=100000,
        recent_real_trades_by_symbol={
            "002463": {"direction": "BUY", "price": 146.0}
        },
    )

    assert len(selected) == 1
    assert selected[0]["alert_source"] == "committee_sell"
    assert suppressed == []


def test_repeated_trade_guard_blocks_buyback_after_sell_without_new_trigger():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "长江电力",
        "verdict": "ACCUMULATE",
        "confidence": 0.72,
        "suggested_alloc_cny": 5000,
    }
    ledger = _DummyLedger([
        {
            "id": 3,
            "ts": "2099-01-01T00:00:00+00:00",
            "trade_date": "2099-01-01",
            "symbol": "600900",
            "direction": "SELL",
            "source": "committee_auto",
            "price": 29.0,
        }
    ])
    state = {
        "symbols": {
            "600900": {
                "entry_exit_points": {
                    "buy_pullback_price": 27.0,
                    "buy_breakout_price": 30.5,
                    "reentry_price": 26.5,
                }
            }
        }
    }

    guarded = apply_repeated_trade_guard(
        result,
        stock={"symbol": "600900", "position_pct": 0.0},
        current_price=29.2,
        ledger=ledger,
        entry_exit_state=state,
    )

    assert guarded["execution_blocked"] is True
    assert guarded["execution_block_reason"] == "recent_opposite_committee_trade_without_new_entry_exit_trigger"


def test_repeated_trade_guard_allows_buyback_after_sell_on_pullback_or_breakout():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "长江电力",
        "verdict": "ACCUMULATE",
        "confidence": 0.72,
        "suggested_alloc_cny": 5000,
    }
    ledger = _DummyLedger([
        {
            "id": 3,
            "ts": "2099-01-01T00:00:00+00:00",
            "trade_date": "2099-01-01",
            "symbol": "600900",
            "direction": "SELL",
            "source": "committee_auto",
            "price": 29.0,
        }
    ])
    state = {
        "symbols": {
            "600900": {
                "entry_exit_points": {
                    "buy_pullback_price": 27.0,
                    "buy_breakout_price": 30.5,
                    "reentry_price": 26.5,
                }
            }
        }
    }

    pullback = apply_repeated_trade_guard(
        result,
        stock={"symbol": "600900", "position_pct": 0.0},
        current_price=26.9,
        ledger=ledger,
        entry_exit_state=state,
    )
    breakout = apply_repeated_trade_guard(
        result,
        stock={"symbol": "600900", "position_pct": 0.0},
        current_price=30.6,
        ledger=ledger,
        entry_exit_state=state,
    )

    assert pullback is result
    assert breakout is result


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


def test_cash_recovery_mode_preserves_cash_by_suppressing_marginal_buy():
    result = {
        "success": True,
        "symbol": "600183",
        "name": "生益科技",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.62,
        "suggested_alloc_cny": 6000,
        "fundamental_score": 75,
        "entry_exit_points": {"reward_risk_ratio": 2.0},
    }

    active, _ = select_optimal_actionable_alerts(
        results=[result],
        prices={"600183": {"price": 20.0, "change_pct": 1.0}},
        cash=10000,
        stocks=[{"symbol": "600183", "position_pct": 0, "min_lot_size": 100}],
        entry_exit_state={},
        portfolio_value=100000,
        trading_mode="active_profit",
    )
    recovery, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"600183": {"price": 20.0, "change_pct": 1.0}},
        cash=10000,
        stocks=[{"symbol": "600183", "position_pct": 0, "min_lot_size": 100}],
        entry_exit_state={},
        portfolio_value=100000,
        trading_mode="cash_recovery",
    )

    assert [row["symbol"] for row in active] == ["600183"]
    assert recovery == []
    assert suppressed[0]["reason"].startswith("cash_reserve_insufficient:cash_recovery")


def test_legacy_risk_off_mode_folds_into_cash_recovery_without_regime_gate():
    result = {
        "success": True,
        "symbol": "600183",
        "name": "生益科技",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.92,
        "suggested_alloc_cny": 30000,
        "fundamental_score": 80,
        "regime": "REGIME: crash",
        "entry_exit_points": {"reward_risk_ratio": 2.0, "atr_pct": 6.0},
    }
    common = dict(
        results=[result],
        prices={"600183": {"price": 20.0, "change_pct": -2.0}},
        cash=100000,
        stocks=[{"symbol": "600183", "position_pct": 0, "min_lot_size": 100}],
        entry_exit_state={},
        portfolio_value=100000,
    )

    cash_recovery, cash_suppressed = select_optimal_actionable_alerts(**common, trading_mode="cash_recovery")
    legacy, legacy_suppressed = select_optimal_actionable_alerts(**common, trading_mode="risk_off")

    assert legacy == cash_recovery
    assert legacy_suppressed[0]["reason"] == cash_suppressed[0]["reason"]
    assert legacy_suppressed[0]["reason"].endswith(":cash_recovery")
    assert "regime_volatility_gate" not in legacy_suppressed[0]


def test_legacy_chinese_risk_off_label_folds_into_cash_recovery():
    assert normalize_trading_mode("主动避险") == "cash_recovery"


@pytest.mark.skip(reason="Position exit discipline is disabled")
def test_cash_recovery_mode_still_waits_for_current_exit_trigger():
    result = {
        "success": True,
        "symbol": "002185",
        "name": "华天科技",
        "market": "a",
        "verdict": "SELL",
        "confidence": 0.30,
        "suggested_alloc_cny": -18000,
        "position_exit_policy": {
            "policy_quality_score": 5.0,
            "sell_count": 30,
            "sell_win_rate_lower": 0.55,
            "avg_sell_win_cny": 600.0,
            "avg_sell_loss_cny": 320.0,
            "avg_post_sell_avoided_drawdown_pct": 4.0,
            "avg_post_sell_missed_rebound_pct": 1.5,
            "avg_post_sell_net_edge_pct": 2.5,
            "conservative_sell_expectancy_cny": 220.0,
        },
    }
    stock = {"symbol": "002185", "position_pct": 16.0, "units": 1600, "cost": 18.0}
    price = {"price": 18.0, "change_pct": -1.0}
    state = {"symbols": {"002185": {"position_exit_plan": {"effective_stop_price": 16.0}}}}

    active, _ = select_optimal_actionable_alerts(
        results=[result],
        prices={"002185": price},
        cash=10000,
        stocks=[stock],
        entry_exit_state=state,
        portfolio_value=100000,
        trading_mode="active_profit",
    )
    recovery, _ = select_optimal_actionable_alerts(
        results=[result],
        prices={"002185": price},
        cash=10000,
        stocks=[stock],
        entry_exit_state=state,
        portfolio_value=100000,
        trading_mode="cash_recovery",
    )

    assert active == []
    assert recovery == []


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
        hk_spatio_factor={"model_key": "hk_spatio_temporal_momentum_proxy_v1"},
    )

    assert result == {"success": True, "symbol": "600900", "verdict": "HOLD"}
    assert captured["req"].symbol == "600900"
    assert captured["req"].holdings[0].weight_pct == 5.0
    assert captured["req"].hk_spatio_factor["model_key"] == "hk_spatio_temporal_momentum_proxy_v1"

@pytest.mark.skip(reason="Position exit discipline is disabled")
def test_policy_quality_adjusts_existing_position_sell_score_conservatively():
    from jobs.market_monitor import _action_score

    base = {
        "success": True,
        "symbol": "600900",
        "verdict": "TRIM",
        "confidence": 0.50,
        "suggested_alloc_cny": -10000,
        "position_exit_policy": {
            "policy_quality_score": 8.0,
            "sell_count": 20,
            "sell_win_rate_lower": 0.62,
            "conservative_sell_expectancy_cny": 350.0,
        },
    }
    stock = {"symbol": "600900", "position_pct": 10.0, "units": 1000}
    price = {"price": 10.0, "change_pct": 0.0}
    triggers = [{"side": "sell", "kind": "take_profit_1", "level": 10.8, "price": 10.9}]

    high_quality = _action_score(base, stock=stock, price_info=price, triggers=triggers)
    low_quality = _action_score(
        {**base, "position_exit_policy": {"policy_quality_score": -8.0, "sell_count": 20, "sell_win_rate_lower": 0.25, "conservative_sell_expectancy_cny": -350.0}},
        stock=stock,
        price_info=price,
        triggers=triggers,
    )

    assert high_quality > low_quality
    assert high_quality - low_quality <= 8.1


@pytest.mark.skip(reason="Position exit discipline is disabled")
def test_sell_trigger_uses_policy_aware_threshold_below_plain_45():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "长江电力",
        "market": "a",
        "verdict": "TRIM",
        "confidence": 0.19,
        "suggested_alloc_cny": -5000,
        "position_exit_policy": {
            "policy_quality_score": 6.0,
            "sell_count": 30,
            "sell_win_rate_lower": 0.60,
            "avg_sell_win_cny": 600.0,
            "avg_sell_loss_cny": 300.0,
            "avg_post_sell_avoided_drawdown_pct": 6.0,
            "avg_post_sell_missed_rebound_pct": 2.0,
            "avg_post_sell_net_edge_pct": 4.0,
            "profit_factor": 1.8,
            "conservative_sell_expectancy_cny": 300.0,
        },
    }
    triggers = [{"side": "sell", "kind": "position_stop", "level": 10.0, "price": 9.9}]

    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"600900": {"price": 9.9, "change_pct": -1.0}},
        cash=10000,
        stocks=[{"symbol": "600900", "position_pct": 8.0, "units": 1000, "cost": 10.0}],
        entry_exit_state={
            "symbols": {
                "600900": {
                    "position_exit_plan": {
                        "version": 1,
                        "entry_price": 10.0,
                        "units": 1000,
                        "effective_stop_price": 10.0,
                        "hard_stop_price": 10.0,
                    }
                }
            }
        },
    )

    assert suppressed == []
    assert selected[0]["symbol"] == "600900"
    assert selected[0]["alert_threshold"] < 45.0
    assert selected[0]["alert_score"] >= selected[0]["alert_threshold"]


@pytest.mark.skip(reason="Position exit discipline is disabled")
def test_sector_panic_guard_delays_mechanical_stop_when_stock_follows_sector():
    result = {
        "success": True,
        "symbol": "002185",
        "name": "华天科技",
        "market": "a",
        "verdict": "SELL",
        "confidence": 0.82,
        "suggested_alloc_cny": -26000,
        "position_exit_policy": {
            "max_loss_pct": 4.0,
            "sell_reliability": 0.8,
            "sell_win_rate_lower": 0.54,
            "post_sell_positive_edge_lower": 0.52,
            "avg_post_sell_missed_rebound_pct": 6.0,
            "avg_post_sell_avoided_drawdown_pct": 1.5,
            "avg_post_sell_net_edge_pct": 0.4,
        },
    }
    stocks = [
        {"symbol": "002185", "sector": "半导体", "position_pct": 8.0, "units": 1600, "cost": 10.0},
        {"symbol": "688099", "sector": "半导体"},
        {"symbol": "603986", "sector": "半导体"},
        {"symbol": "688012", "sector": "半导体"},
        {"symbol": "600900", "sector": "电力行业"},
        {"symbol": "601398", "sector": "银行"},
        {"symbol": "600519", "sector": "白酒"},
    ]
    prices = {
        "002185": {"price": 9.40, "change_pct": -5.4},
        "688099": {"price": 58.0, "change_pct": -5.8},
        "603986": {"price": 69.0, "change_pct": -5.2},
        "688012": {"price": 42.0, "change_pct": -5.6},
        "600900": {"price": 29.0, "change_pct": -0.2},
        "601398": {"price": 6.0, "change_pct": 0.1},
        "600519": {"price": 1400.0, "change_pct": -0.4},
    }
    state = {
        "symbols": {
            "002185": {
                "position_exit_plan": {
                    "effective_stop_price": 9.80,
                    "hard_stop_price": 9.80,
                    "entry_price": 10.0,
                    "units": 1600,
                    "version": 1,
                },
                "last_triggers": [{"side": "sell", "kind": "position_stop", "level": 9.8, "price": 9.5}],
            }
        }
    }

    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices=prices,
        cash=10000,
        stocks=stocks,
        entry_exit_state=state,
        max_alerts=4,
    )

    assert selected == []
    assert suppressed[0]["reason"].startswith("sector_panic_guard:")
    guard = suppressed[0]["discipline_review"]["sector_panic_guard"]
    assert guard["active"] is True
    assert guard["sector"] == "半导体"
    assert guard["target_relative_z"] >= -1.0


@pytest.mark.skip(reason="Position exit discipline is disabled")
def test_sector_panic_guard_does_not_block_idiosyncratic_sector_laggard():
    result = {
        "success": True,
        "symbol": "002185",
        "name": "华天科技",
        "market": "a",
        "verdict": "HOLD",
        "confidence": 0.52,
        "suggested_alloc_cny": 0,
        "position_exit_policy": {
            "max_loss_pct": 4.0,
            "sell_reliability": 0.8,
            "sell_win_rate_lower": 0.58,
            "post_sell_positive_edge_lower": 0.58,
            "avg_post_sell_missed_rebound_pct": 1.0,
            "avg_post_sell_avoided_drawdown_pct": 4.0,
            "avg_post_sell_net_edge_pct": 3.0,
        },
    }
    stocks = [
        {"symbol": "002185", "sector": "半导体", "position_pct": 8.0, "units": 1600, "cost": 10.0},
        {"symbol": "688099", "sector": "半导体"},
        {"symbol": "603986", "sector": "半导体"},
        {"symbol": "688012", "sector": "半导体"},
        {"symbol": "600900", "sector": "电力行业"},
    ]
    prices = {
        "002185": {"price": 9.10, "change_pct": -8.0},
        "688099": {"price": 58.0, "change_pct": -3.2},
        "603986": {"price": 69.0, "change_pct": -3.1},
        "688012": {"price": 42.0, "change_pct": -3.4},
        "600900": {"price": 29.0, "change_pct": -0.2},
    }
    state = {
        "symbols": {
            "002185": {
                "position_exit_plan": {
                    "effective_stop_price": 9.80,
                    "hard_stop_price": 9.80,
                    "entry_price": 10.0,
                    "units": 1600,
                    "version": 1,
                },
                "last_triggers": [{"side": "sell", "kind": "position_stop", "level": 9.8, "price": 9.4}],
            }
        }
    }

    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices=prices,
        cash=10000,
        stocks=stocks,
        entry_exit_state=state,
        max_alerts=4,
    )

    assert suppressed == []
    assert selected[0]["symbol"] == "002185"
    guard = selected[0]["discipline_review"]["sector_panic_guard"]
    assert guard["active"] is False
    assert guard["reason"] == "target_weaker_than_sector"


@pytest.mark.skip(reason="Position exit discipline is disabled")
def test_high_confidence_held_sell_waits_without_current_exit_trigger():
    result = {
        "success": True,
        "symbol": "002185",
        "name": "华天科技",
        "market": "a",
        "verdict": "SELL",
        "confidence": 0.66,
        "suggested_alloc_cny": -12000,
        "position_exit_policy": {
            "policy_quality_score": 7.0,
            "sell_count": 36,
            "sell_win_rate_lower": 0.58,
            "avg_sell_win_cny": 900.0,
            "avg_sell_loss_cny": 350.0,
            "avg_post_sell_avoided_drawdown_pct": 5.0,
            "avg_post_sell_missed_rebound_pct": 1.5,
            "avg_post_sell_net_edge_pct": 3.5,
            "conservative_sell_expectancy_cny": 360.0,
        },
    }
    stock = {"symbol": "002185", "position_pct": 18.0, "units": 1600, "cost": 18.0}
    price = {"price": 18.2, "change_pct": -1.6}

    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"002185": price},
        cash=10000,
        stocks=[stock],
        entry_exit_state={"symbols": {"002185": {"position_exit_plan": {"effective_stop_price": 16.5}}}},
        max_alerts=4,
    )

    assert _sell_committee_execution_edge(result, stock, price) > 0.45
    assert selected == []
    assert suppressed[0]["symbol"] == "002185"
    assert suppressed[0]["reason"].startswith("sell_waiting_for_current_exit_trigger:")


@pytest.mark.skip(reason="Position exit discipline is disabled")
def test_weak_trim_without_trigger_stays_candidate_with_clear_reason():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "长江电力",
        "market": "a",
        "verdict": "TRIM",
        "confidence": 0.28,
        "suggested_alloc_cny": -500,
        "position_exit_policy": {
            "policy_quality_score": -2.0,
            "sell_count": 4,
            "sell_win_rate_lower": 0.22,
            "avg_sell_win_cny": 100.0,
            "avg_sell_loss_cny": 300.0,
            "avg_post_sell_avoided_drawdown_pct": 1.0,
            "avg_post_sell_missed_rebound_pct": 3.0,
            "avg_post_sell_net_edge_pct": -2.0,
            "conservative_sell_expectancy_cny": -120.0,
        },
    }

    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"600900": {"price": 28.0, "change_pct": 0.3}},
        cash=10000,
        stocks=[{"symbol": "600900", "position_pct": 5.0, "units": 500, "cost": 27.5}],
        entry_exit_state={"symbols": {"600900": {"position_exit_plan": {"effective_stop_price": 24.0}}}},
        max_alerts=4,
    )

    assert selected == []
    assert suppressed
    assert suppressed[0]["reason"].startswith("sell_waiting_for_current_exit_trigger:")


@pytest.mark.skip(reason="Position exit discipline is disabled")
def test_stale_position_stop_is_recomputed_from_current_holding_cost_before_alerting():
    result = {
        "success": True,
        "symbol": "002463",
        "name": "沪电股份",
        "market": "a",
        "verdict": "SELL",
        "confidence": 0.66,
        "suggested_alloc_cny": -28802,
        "entry_exit_points": {
            "atr_pct": 7.3373,
            "expected_return_pct": 5.5455,
            "reward_risk_ratio": 1.5,
        },
        "position_exit_policy": {
            "max_loss_pct": 5.5,
            "stop_atr_mult": 1.4,
            "take_profit_r1": 1.0,
            "take_profit_r2": 2.75,
            "sell_reliability": 0.714286,
            "sell_win_rate_lower": 0.365462,
            "post_sell_positive_edge_lower": 0.342082,
        },
    }
    stock = {"symbol": "002463", "position_pct": 8.5, "units": 200, "cost": 146.55, "min_lot_size": 100}
    state = {
        "symbols": {
            "002463": {
                "position_exit_plan": {
                    "entry_price": 152.48,
                    "units": 200,
                    "effective_stop_price": 144.09,
                    "hard_stop_price": 144.09,
                    "take_profit_1_price": 160.0,
                    "take_profit_2_price": 170.0,
                    "version": 1,
                },
                "last_triggers": [{"side": "sell", "kind": "position_stop", "level": 144.09, "price": 144.01}],
            }
        }
    }

    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"002463": {"price": 144.01, "change_pct": -5.75}},
        cash=10000,
        stocks=[stock],
        entry_exit_state=state,
        portfolio_value=342148,
    )

    assert selected == []
    assert suppressed[0]["symbol"] == "002463"
    assert suppressed[0]["reason"].startswith("sell_waiting_for_current_exit_trigger:")


def test_a_share_same_day_buy_units_are_not_sellable_for_alerts():
    result = {
        "success": True,
        "symbol": "002463",
        "name": "沪电股份",
        "market": "a",
        "verdict": "SELL",
        "confidence": 0.82,
        "suggested_alloc_cny": -26000,
        "entry_exit_points": {"expected_return_pct": 0.2, "reward_risk_ratio": 1.2},
        "position_exit_policy": {
            "sell_count": 30,
            "sell_reliability": 0.8,
            "sell_win_rate_lower": 0.6,
            "post_sell_positive_edge_lower": 0.6,
            "sell_utility_adjustment_pct": 3.0,
            "avg_post_sell_net_edge_pct": 4.0,
            "max_loss_pct": 4.0,
        },
    }
    stock = {"symbol": "002463", "position_pct": 8.5, "units": 200, "cost": 146.55, "min_lot_size": 100}
    state = {
        "symbols": {
            "002463": {
                "position_exit_plan": {
                    "effective_stop_price": 138.49,
                    "hard_stop_price": 138.49,
                    "take_profit_1_price": 154.61,
                    "take_profit_2_price": 168.72,
                    "entry_price": 146.55,
                    "units": 200,
                    "version": 1,
                },
                "last_triggers": [{"side": "sell", "kind": "position_stop", "level": 138.49, "price": 130.0}],
            }
        }
    }

    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"002463": {"price": 130.0, "change_pct": -7.0}},
        cash=10000,
        stocks=[stock],
        entry_exit_state=state,
        portfolio_value=342148,
        same_day_bought_units_by_symbol={"002463": 200},
    )

    assert selected == []
    assert suppressed[0]["reason"] == "same_day_a_share_t1_sell_blocked"


def test_alert_optimizer_blocks_buy_after_recent_real_sell_without_new_trigger():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "长江电力",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.86,
        "suggested_alloc_cny": 10000,
        "entry_exit_points": {"expected_return_pct": 6.0, "reward_risk_ratio": 2.2},
    }
    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"600900": {"price": 29.2, "change_pct": 0.3}},
        cash=20000,
        stocks=[{"symbol": "600900", "position_pct": 0.0, "min_lot_size": 100}],
        entry_exit_state={
            "symbols": {
                "600900": {
                    "entry_exit_points": {
                        "buy_pullback_price": 27.0,
                        "buy_breakout_price": 30.5,
                        "reentry_price": 26.5,
                    }
                }
            }
        },
        portfolio_value=100000,
        recent_real_trades_by_symbol={
            "600900": {"symbol": "600900", "direction": "SELL", "price": 29.0, "trade_date": "2099-01-01"}
        },
    )

    assert selected == []
    assert suppressed[0]["reason"] == "recent_opposite_real_trade_without_new_entry_exit_trigger:BUY"


def test_alert_optimizer_allows_buy_after_recent_real_sell_on_fresh_trigger():
    result = {
        "success": True,
        "symbol": "600900",
        "name": "长江电力",
        "market": "a",
        "verdict": "ACCUMULATE",
        "confidence": 0.86,
        "suggested_alloc_cny": 10000,
        "entry_exit_points": {"expected_return_pct": 6.0, "reward_risk_ratio": 2.2},
    }
    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"600900": {"price": 26.9, "change_pct": -1.5}},
        cash=20000,
        stocks=[{"symbol": "600900", "position_pct": 0.0, "min_lot_size": 100}],
        entry_exit_state={
            "symbols": {
                "600900": {
                    "entry_exit_points": {
                        "buy_pullback_price": 27.0,
                        "buy_breakout_price": 30.5,
                        "reentry_price": 26.5,
                    }
                }
            }
        },
        portfolio_value=100000,
        recent_real_trades_by_symbol={
            "600900": {"symbol": "600900", "direction": "SELL", "price": 29.0, "trade_date": "2099-01-01"}
        },
    )

    assert suppressed == []
    assert selected[0]["symbol"] == "600900"


def test_candidate_sell_is_not_rendered_as_plain_observation():
    assert _operation_summary({
        "state": "candidate",
        "operation": {"status": "candidate", "verdict": "TRIM", "suggested_alloc_cny": -5000},
    }) == "待确认卖"


def test_sector_cache_overrides_runtime_a_share_sector_without_mutating_source():
    source = [
        {
            "symbol": "301377",
            "name": "鼎泰高科",
            "market": "a",
            "sector": "高端制造",
            "industry": "PCB钻针",
        },
        {
            "symbol": "00700",
            "name": "腾讯控股",
            "market": "hk",
            "sector": "互联网",
        },
    ]

    updated = apply_sector_cache_to_stocks(source, {"301377": "机械设备", "00700": "互联网服务"})

    assert source[0]["sector"] == "高端制造"
    assert updated[0]["sector"] == "机械设备"
    assert updated[0]["config_sector"] == "高端制造"
    assert updated[0]["sector_source"] == "eastmoney_sector_cache"
    assert updated[1]["sector"] == "互联网"


@pytest.mark.skip(reason="Position exit discipline is disabled")
def test_hold_with_confirmed_take_profit_2_can_become_discipline_sell_alert():
    result = {
        "success": True,
        "symbol": "301377",
        "name": "鼎泰高科",
        "market": "a",
        "verdict": "HOLD",
        "confidence": 0.52,
        "suggested_alloc_cny": 0,
        "fundamental_score": 58,
        "entry_exit_points": {"expected_return_pct": 0.5, "reward_risk_ratio": 1.2},
        "position_exit_policy": {
            "sell_count": 42,
            "sell_reliability": 0.78,
            "sell_win_rate_lower": 0.59,
            "post_sell_positive_edge_lower": 0.56,
            "sell_utility_adjustment_pct": 3.2,
            "avg_post_sell_net_edge_pct": 4.0,
            "max_loss_pct": 4.0,
        },
    }
    stock = {"symbol": "301377", "position_pct": 20.0, "units": 100, "cost": 457.59, "min_lot_size": 100}
    state = {
        "symbols": {
            "301377": {
                "position_exit_plan": {
                    "effective_stop_price": 430.0,
                    "take_profit_1_price": 494.2,
                    "take_profit_2_price": 535.38,
                },
                "last_triggers": [{"side": "sell", "kind": "take_profit_2", "level": 535.38, "price": 548.0}],
            }
        }
    }

    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"301377": {"price": 548.0, "change_pct": 1.2}},
        cash=10000,
        stocks=[stock],
        entry_exit_state=state,
        portfolio_value=100000,
    )

    assert suppressed == []
    assert selected[0]["symbol"] == "301377"
    assert selected[0]["verdict"] == "SELL"
    assert selected[0]["committee_verdict"] == "HOLD"
    assert selected[0]["suggested_alloc_cny"] == -54800.0
    assert selected[0]["alert_selected_lots"] == 1
    assert selected[0]["alert_source"] == "position_exit_discipline_review"
    review = selected[0]["discipline_review"]
    assert review["model"] == "triggered_exit_expected_utility_v1"
    assert review["decision"] == "execute"
    assert review["expected_utility_edge_pct"] > 0
    assert review["sell_win_rate_lower"] == 0.59


@pytest.mark.skip(reason="Position exit discipline is disabled")
def test_hold_take_profit_waits_when_only_one_round_or_continuation_edge_wins():
    result = {
        "success": True,
        "symbol": "301377",
        "name": "鼎泰高科",
        "market": "a",
        "verdict": "HOLD",
        "confidence": 0.88,
        "suggested_alloc_cny": 0,
        "fundamental_score": 86,
        "right_side_trend_gate": {"allow": True, "reason": "趋势仍强"},
        "entry_exit_points": {"expected_return_pct": 7.0, "reward_risk_ratio": 3.0},
        "position_exit_policy": {
            "sell_count": 3,
            "sell_reliability": 0.12,
            "sell_win_rate_lower": 0.20,
            "post_sell_positive_edge_lower": 0.18,
            "sell_utility_adjustment_pct": -1.0,
            "avg_post_sell_net_edge_pct": -2.0,
        },
    }
    state = {
        "symbols": {
            "301377": {
                "position_exit_plan": {
                    "effective_stop_price": 430.0,
                    "take_profit_1_price": 494.2,
                    "take_profit_2_price": 535.38,
                },
                "last_triggers": [],
            }
        }
    }

    selected, suppressed = select_optimal_actionable_alerts(
        results=[result],
        prices={"301377": {"price": 548.0, "change_pct": 3.0}},
        cash=10000,
        stocks=[{"symbol": "301377", "position_pct": 20.0, "units": 100, "cost": 457.59, "min_lot_size": 100}],
        entry_exit_state=state,
        portfolio_value=100000,
    )

    assert selected == []
    assert suppressed[0]["alert_source"] == "position_exit_discipline_review"
    assert suppressed[0]["reason"].startswith("discipline_review_wait:")
    assert suppressed[0]["discipline_review"]["decision"] == "review"
    assert _operation_summary({
        "state": "candidate",
        "operation": {
            "status": "candidate",
            "verdict": "HOLD",
            "discipline_review": suppressed[0]["discipline_review"],
        },
    }) == "止盈复核"


@pytest.mark.skip(reason="Position exit discipline is disabled")
def test_snapshot_preserves_committee_verdict_for_discipline_sell_alert():
    result = {
        "success": True,
        "symbol": "301377",
        "name": "鼎泰高科",
        "market": "a",
        "verdict": "HOLD",
        "confidence": 0.52,
        "suggested_alloc_cny": 0,
        "position_exit_policy": {},
    }
    action = {
        **result,
        "verdict": "SELL",
        "committee_verdict": "HOLD",
        "suggested_alloc_cny": -54800,
        "alert_source": "position_exit_discipline_review",
        "alert_selected_lots": 1,
        "optimizer_lots": 1,
        "llm_review_lots": 1,
        "discipline_review": {
            "model": "triggered_exit_expected_utility_v1",
            "trigger_kind": "take_profit_2",
            "expected_utility_edge_pct": 1.25,
            "committee_verdict": "HOLD",
        },
    }

    snapshot = build_monitor_window_snapshot(
        round_time="10:00",
        results=[result],
        actionable=[action],
        prices={"301377": {"price": 548.0, "change_pct": 1.2}},
        stocks=[{"symbol": "301377", "name": "鼎泰高科", "position_pct": 20.0, "units": 100, "cost": 457.59}],
        entry_exit_watch=[{
            "symbol": "301377",
            "name": "鼎泰高科",
            "confirmed": True,
            "triggers": [{"side": "sell", "kind": "take_profit_2", "level": 535.38, "price": 548.0}],
            "position_exit_plan": {"take_profit_2_price": 535.38, "take_profit_1_price": 494.2, "effective_stop_price": 430.0},
        }],
        entry_exit_alerts=[],
        suppressed_alerts=[],
        cash=10000,
        total_assets=100000,
    )

    op = snapshot["rows"][0]["operation"]
    assert op["status"] == "action_required"
    assert op["verdict"] == "SELL"
    assert op["committee_verdict"] == "HOLD"
    assert op["alert_source"] == "position_exit_discipline_review"
    assert op["discipline_review"]["expected_utility_edge_pct"] == 1.25
    assert _operation_summary(snapshot["rows"][0]) == "止盈卖1手"


def test_sell_policy_stats_shrink_small_sample_win_rate():
    from scripts.backtest_ashare_committee_exit import (
        _policy_quality_score,
        _sell_evidence_adjustment,
        _sell_policy_stats,
    )

    stats = _sell_policy_stats([100.0, 80.0])
    assert stats["sell_win_rate"] == 1.0
    assert stats["sell_win_rate_lower"] < 0.7

    metrics = {
        **stats,
        "objective_score": 1.0,
        "max_drawdown_pct": -5.0,
    }
    assert _policy_quality_score(metrics) < 2.0

    enriched = {**metrics, "sell_path_sample_count": 2, "avg_post_sell_net_edge_pct": 3.0, "post_sell_positive_edge_lower": 0.34}
    adjustment = _sell_evidence_adjustment(enriched)
    assert 0.0 < adjustment["sell_reliability"] < 0.5
    assert abs(adjustment["sell_utility_adjustment_pct"]) < 2.0

