import sys
import types
from datetime import datetime

from jobs.market_monitor import (
    apply_repeated_trade_guard,
    build_monitor_window_snapshot,
    call_committee,
    evaluate_entry_exit_triggers,
    is_scheduled_monitor_popup_time,
    select_optimal_actionable_alerts,
    should_send_monitor_summary_popup,
    update_entry_exit_alert_state,
)


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
        actionable=[{**result, "alert_score": 80}],
        prices={"600900": {"price": 10.6, "prev_close": 10.0, "change_pct": 6.0}},
        stocks=[{"symbol": "600900", "name": "长江电力", "sector": "电力", "position_pct": 0}],
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
