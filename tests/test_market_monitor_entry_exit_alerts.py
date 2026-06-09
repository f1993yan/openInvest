from jobs.market_monitor import (
    apply_repeated_trade_guard,
    evaluate_entry_exit_triggers,
    select_optimal_actionable_alerts,
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
