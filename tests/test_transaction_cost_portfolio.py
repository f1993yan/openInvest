from datetime import datetime
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from core.transaction_cost_portfolio import MODEL_KEY, PARAMETERS, historical_covariance, optimize_transaction_cost_portfolio
from services.ashare_allocation import prepare_account_plan, plan_decision, reconcile_plan, preview_active_decision
from jobs.market_monitor_alerts import select_optimal_actionable_alerts


NOW = datetime.fromisoformat("2026-10-30T10:00:00+08:00")


def histories(symbol):
    dates = pd.bdate_range(end="2026-10-29", periods=180)
    return pd.DataFrame({"Close": 10*np.exp(np.arange(180)*0.001+0.01*np.sin(np.arange(180)))}, index=dates)


def factors():
    return {s: {"selected": True, "expected_return_pct": 8, "target_weight_pct": 23.75,
                "target_revision": "2026-10-26", "low_confidence": False}
            for s in ["600001", "600002", "600003", "600004"]}


def test_joint_self_financing_and_outside_target_full_exit():
    symbols = ["600001", "600002", "600003", "600004", "600005"]
    result = optimize_transaction_cost_portfolio(symbols=symbols,
        current_weights={"600005": .8}, selected=set(symbols[:4]),
        expected_returns_pct={s: 8 for s in symbols}, covariance=np.eye(5)*.02)
    assert result["weights"]["600005"] == 0
    assert max(result["weights"].values()) <= .35+1e-8
    assert sum(result["weights"].values())+result["cash_weight"]+result["estimated_cost_weight"] == pytest.approx(1)
    assert result["cash_weight"] >= .05*(1-result["estimated_cost_weight"])-1e-8


def test_cash_mode_and_foreign_positions_have_separate_budget():
    result = optimize_transaction_cost_portfolio(symbols=["600001"], current_weights={},
        selected={"600001"}, expected_returns_pct={"600001": 15}, covariance=np.eye(1)*.001,
        budget_fraction=.6, fixed_weight=.4, cash_reserve=.35)
    assert sum(result["weights"].values()) < .251
    assert result["cash_weight"] >= .349


def test_foreign_locked_assets_do_not_make_forced_a_share_exit_infeasible():
    result = optimize_transaction_cost_portfolio(symbols=["600001"],
        current_weights={"600001": .02}, selected=set(), expected_returns_pct={},
        covariance=np.zeros((1, 1)), budget_fraction=.03, fixed_weight=.97, cash_reserve=.05)
    assert result["weights"]["600001"] == 0
    assert result["cash_weight"]+result["estimated_cost_weight"] == pytest.approx(.03)
    assert result["cash_reserve_weight_floor"] < .03


def test_point_in_time_covariance_ignores_future():
    frame = histories("a")
    before = historical_covariance({"a": frame}, ["a"], as_of="2026-10-20")
    frame.loc[frame.index > "2026-10-20", "Close"] = 1000000
    after = historical_covariance({"a": frame}, ["a"], as_of="2026-10-20")
    np.testing.assert_allclose(before, after)


def test_plan_persists_and_manual_cash_or_universe_changes_do_not_reopen(tmp_path):
    path = tmp_path/"allocation.json"
    fs = factors()
    prices = {s: {"price": 10} for s in fs}
    plan = prepare_account_plan(account="real", holdings=[], trades=[], cash=100000,
        pending_cash=0, prices=prices, factors=fs, state_path=path, now=NOW, history_loader=histories)
    symbol, order = next((s, o) for s, o in plan["orders"].items() if o["remaining_units"] > 0)
    units = order["remaining_units"]
    trades = [{"id": 1, "symbol": symbol, "direction": "BUY", "units": units,
               "trade_date": "2026-10-29", "source": "user_explicit"},
              {"id": 2, "symbol": symbol, "direction": "SELL", "units": units,
               "trade_date": "2026-10-30", "source": "user_explicit"}]
    fs["600099"] = {"selected": False, "low_confidence": False}
    again = prepare_account_plan(account="real", holdings=[], trades=trades, cash=200000,
        pending_cash=0, prices=prices, factors=fs, state_path=path, now=NOW,
        history_loader=lambda s: pytest.fail("existing plan must not refetch/reoptimize"))
    assert again["revision"] == plan["revision"]
    assert again["orders"][symbol]["remaining_units"] == 0
    assert plan_decision(again, symbol=symbol, price_info=prices[symbol], holdings=[], trades=trades,
        cash=200000, now=NOW)["units"] == 0


def test_partial_fill_remainder_and_opposite_trade_cancellation():
    plan = {"trade_cursor": 4, "orders": {"600001": {"remaining_units": 500}}, "observed_units": {"600001": 0}}
    trades = [{"id": 5, "symbol": "600001", "direction": "BUY", "units": 200}]
    p = reconcile_plan(plan, trades, [{"symbol": "600001", "units": 200}])
    assert p["orders"]["600001"]["remaining_units"] == 300
    assert reconcile_plan(p, trades, [{"symbol": "600001", "units": 200}])["orders"]["600001"]["remaining_units"] == 300
    trades.append({"id": 6, "symbol": "600001", "direction": "SELL", "units": 100, "source": "user_explicit"})
    assert reconcile_plan(p, trades, [{"symbol": "600001", "units": 100}])["orders"]["600001"]["remaining_units"] == 0


def test_t1_market_hours_limits_and_missing_state(tmp_path):
    plan = {"revision": "x", "week": [2026, 44], "effective_at": "2026-10-26T09:30:00+08:00",
            "orders": {"600001": {"remaining_units": -150}}, "observed_units": {"600001": 150}}
    holdings = [{"symbol": "600001", "units": 150}]
    trades = [{"id": 1, "symbol": "600001", "direction": "BUY", "units": 150, "trade_date": "2026-10-30"}]
    plan["trade_cursor"] = 1
    common = dict(symbol="600001", price_info={"price": 10}, holdings=holdings, cash=0)
    assert plan_decision(plan, **common, trades=trades, now=NOW)["units"] == 0
    common["price_info"]["change_pct"] = -10
    assert plan_decision(plan, **common, trades=[], now=NOW)["units"] == 0
    common["price_info"]["change_pct"] = 0
    assert plan_decision(plan, **common, trades=[], now=NOW)["units"] == 150
    assert plan_decision(plan, **common, trades=[], now=NOW.replace(hour=17))["units"] == 0
    decision = preview_active_decision("600001", account="real", price_info={"price": 10}, state_path=tmp_path/"missing")
    assert decision["verdict"] == "WAIT"


def test_cash_cannot_spend_unexecuted_sales_and_plan_sells_bypass_old_score():
    def result(s, verdict, units):
        return {"symbol": s, "market": "a", "success": True, "confidence": .1, "verdict": verdict,
                "suggested_alloc_cny": units*10, "portfolio_optimizer": {"model": MODEL_KEY,
                "units": abs(units), "cash_reserve": .05, "equity_at_creation": 100000}}
    sell, buy = result("600001", "SELL", -1000), result("600002", "BUY", 2000)
    selected, suppressed = select_optimal_actionable_alerts(results=[buy, sell],
        prices={s: {"price": 10} for s in ["600001", "600002"]}, cash=5000,
        stocks=[{"symbol": "600001", "units": 1000}], entry_exit_state={})
    assert [r["symbol"] for r in selected] == ["600001"]
    assert suppressed[0]["symbol"] == "600002"


def test_new_activation_changes_plan_revision(tmp_path):
    fs = factors()
    common = dict(account="real", holdings=[], trades=[], cash=100000, pending_cash=0,
                  prices={s: {"price": 10} for s in fs}, state_path=tmp_path/"p.json", now=NOW, history_loader=histories)
    before = prepare_account_plan(**common, factors=fs)
    for factor in fs.values():
        factor["target_revision"] = "manual_refresh_20261030"
    after = prepare_account_plan(**common, factors=fs)
    assert before["revision"] != after["revision"]


def test_unconstrained_single_asset_matches_analytic_optimum():
    params = replace(PARAMETERS, fee_rate=0, slippage_rate=0)
    result = optimize_transaction_cost_portfolio(symbols=["600001"],
        current_weights={"600001": .10}, selected={"600001"},
        expected_returns_pct={"600001": 5}, covariance=np.array([[.05]]), parameters=params)
    expected = (.05*5/20 + params.l2_turnover*.10)/(params.risk_aversion*.05+params.l2_turnover)
    assert result["weights"]["600001"] == pytest.approx(expected, abs=1e-5)


def test_turnover_cost_creates_no_trade_region():
    # The marginal expected gain is smaller than the marginal transaction cost.
    result = optimize_transaction_cost_portfolio(symbols=["600001"], current_weights={},
        selected={"600001"}, expected_returns_pct={"600001": .1}, covariance=np.array([[.01]]))
    assert result["weights"]["600001"] < 1e-6


def test_unknown_new_watch_member_does_not_block_existing_top4_or_exit(tmp_path):
    fs = factors()
    fs["600099"] = {"selected": False, "low_confidence": True}
    held = [{"symbol": "600098", "market": "a", "units": 150}]
    prices = {s: {"price": 10} for s in fs}
    prices["600098"] = {"price": 10}
    def loader(s):
        assert s in set(factors()), "zero-weight exits need no risk history"
        return histories(s)
    plan = prepare_account_plan(account="real", holdings=held, trades=[], cash=100000,
        pending_cash=0, prices=prices, factors=fs, state_path=tmp_path/"p.json", now=NOW, history_loader=loader)
    assert plan["orders"]["600098"]["remaining_units"] == -150


def test_stale_history_and_corrupt_state_fail_closed(tmp_path):
    common = dict(account="real", holdings=[], trades=[], cash=100000, pending_cash=0,
                  prices={s: {"price": 10} for s in factors()}, factors=factors(), now=NOW)
    path = tmp_path/"p.json"
    with pytest.raises(ValueError, match="stale_portfolio_history"):
        prepare_account_plan(**common, state_path=path, history_loader=lambda s: histories(s).iloc[:-3])
    path.write_text('{"accounts": null}', encoding="utf-8")
    assert preview_active_decision("600001", account="real", price_info={"price": 10}, state_path=path)["verdict"] == "WAIT"


def test_st_limit_and_cash_reserve_in_plan_preview():
    plan = {"model": MODEL_KEY, "account": "real", "revision": "r", "week": [2026, 44],
        "effective_at": "2026-10-26T09:30:00+08:00", "cash_reserve": .05,
        "equity_at_creation": 100000, "orders": {"600001": {"remaining_units": -150}}}
    decision = plan_decision(plan, symbol="600001", price_info={"price": 10, "change_pct": -5},
        holdings=[{"symbol": "600001", "name": "ST test", "units": 150}], trades=[], cash=0, now=NOW)
    assert decision["reason"] == "limit_down_sell_blocked"
    plan["orders"]["600001"]["remaining_units"] = 1000
    decision = plan_decision(plan, symbol="600001", price_info={"price": 10}, holdings=[],
        trades=[], cash=5000, now=NOW)
    assert decision["units"] == 0


def test_portfolio_email_reason_uses_action_explanation():
    from jobs.market_monitor_notify import _review_one_line
    assert _review_one_line({"portfolio_optimizer": {"model": MODEL_KEY},
        "cio_memo": "[ALGORITHM_ONLY_MODE]", "optimizer_review": "ONE_LINE: legacy note",
        "decision_synthesis": {"primary_reason": "本周退出前四"}}) == "本周退出前四"


@pytest.mark.parametrize("fresh_revision,fresh_units", [("new", 100), ("old", 0)])
def test_stale_desktop_card_cannot_record_an_outdated_plan(tmp_path, monkeypatch, fresh_revision, fresh_units):
    import db.account_ledger as accounts
    import jobs.market_monitor as monitor
    import services.ashare_allocation as allocation
    from scripts.monitor_window_services import _execute_user_trade_from_row

    ledger_type = accounts.AccountLedger
    db_path = str(tmp_path/"accounts.db")
    config = {"cash": 100000, "total_assets": 100000, "holdings": [],
              "watchlist": [{"symbol": "600001", "market": "a"}]}
    with ledger_type(db_path) as ledger:
        ledger.initialize_from_monitor_config(config)
    monkeypatch.setattr(accounts, "AccountLedger", lambda: ledger_type(db_path))
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "fetch_sina_prices", lambda *a: {"600001": {"price": 10}})
    monkeypatch.setattr(allocation, "enabled", lambda *a: True)
    monkeypatch.setattr(allocation, "STATE_PATH", tmp_path/"p.json")
    monkeypatch.setattr(allocation, "preview_active_decision", lambda *a, **kw:
        {"verdict": "BUY", "units": fresh_units, "plan_revision": fresh_revision})
    row = {"symbol": "600001", "market": "a", "min_lot_size": 100,
        "operation": {"verdict": "BUY", "execution_units": 100, "suggested_alloc_cny": 1000,
            "portfolio_optimizer": {"model": MODEL_KEY, "account": "real", "plan_revision": "old"}}}
    with pytest.raises(ValueError, match="推荐计划或可执行数量已变化"):
        _execute_user_trade_from_row(row, 1)
    with ledger_type(db_path) as ledger:
        assert ledger.list_trades("real") == []


def test_shadow_rechecks_plan_and_cannot_execute_same_fill_twice(tmp_path, monkeypatch):
    from db.account_ledger import AccountLedger
    import services.ashare_allocation as allocation

    path = tmp_path/"p.json"
    monkeypatch.setattr(allocation, "ROOT", tmp_path)
    (tmp_path/"jobs").mkdir()
    (tmp_path/"jobs/market_monitor_config.json").write_text(
        json.dumps({"trading_mode": "active_profit"}), encoding="utf-8")
    monkeypatch.setattr(allocation, "STATE_PATH", path)
    monkeypatch.setattr(allocation, "_local", lambda now=None: NOW)
    with AccountLedger(str(tmp_path/"accounts.db")) as ledger:
        ledger.initialize_from_monitor_config({"cash": 100000, "total_assets": 100000,
            "holdings": [], "watchlist": [{"symbol": s, "market": "a"} for s in factors()]})
        plan = prepare_account_plan(account="committee", holdings=ledger.list_holdings("committee"),
            trades=[], cash=100000, pending_cash=0, prices={s: {"price": 10} for s in factors()},
            factors=factors(), now=NOW, state_path=path, history_loader=histories)
        symbol = next(s for s, o in plan["orders"].items() if o["remaining_units"] > 0)
        decision = preview_active_decision(symbol, account="committee", price_info={"price": 10},
            state_path=path, ledger=ledger, now=NOW)
        result = {"symbol": symbol, "success": True, "verdict": decision["verdict"],
            "suggested_alloc_cny": decision["alloc_cny"], "portfolio_optimizer": decision}
        trade = ledger.apply_committee_result(result, price=10, trade_date="2026-10-30")
        assert trade and trade.units == decision["units"]
        assert ledger.apply_committee_result(result, price=10, trade_date="2026-10-30") is None
        assert ledger.list_trades("real") == []


@pytest.mark.parametrize("llm_mode", [False, True])
def test_backend_portfolio_exit_is_authoritative_and_request_cannot_forge_order(monkeypatch, llm_mode):
    from types import SimpleNamespace
    from backend import server
    from core.ashare_behavioral_factor import AShareBehavioralAssessment
    from core.committee import CommitteeReport
    import core.committee as committee
    import core.regime_probability as probability
    import core.decision_optimizer as old_optimizer
    import utils.akshare_data as data
    import utils.fundamental_data as fundamentals
    import core.buy_signal_miner as miner
    import services.ashare_allocation as allocation

    monkeypatch.setattr(server, "_resolve_market_data", lambda *a: "test")
    monkeypatch.setattr(server, "_save_committee_cache", lambda **kw: None)
    monkeypatch.setattr(server, "_resolve_a_share_behavioral_assessment", lambda *a, **kw:
        AShareBehavioralAssessment(symbol="600001", low_confidence=False))
    monkeypatch.setattr(data, "get_history_data", lambda *a, **kw: pd.DataFrame())
    monkeypatch.setattr(data, "get_macro_data", lambda: "")
    monkeypatch.setattr(fundamentals, "get_fundamental_snapshot", lambda *a: {})
    monkeypatch.setattr(miner, "mine_historical_buy_signals", lambda *a: SimpleNamespace(as_dict=lambda: {}))
    monkeypatch.setattr(probability, "build_probability_table_from_ohlc", lambda *a: {})
    monkeypatch.setattr(probability, "get_regime_probability", lambda *a, **kw: None)
    monkeypatch.setattr(probability, "get_conditional_return_stats", lambda *a: None)
    monkeypatch.setattr(committee, "run_wealth_context_view", lambda *a: "")
    monkeypatch.setattr(committee, "run_macro_view", lambda *a: "")
    monkeypatch.setattr(committee, "run_committee", lambda **kw:
        {"report": CommitteeReport(asset=kw["asset"], cio_memo="VERDICT: HOLD\nCONFIDENCE: 0.5\nALLOC_CNY: 0")})
    monkeypatch.setattr(old_optimizer, "optimize_committee_decision", lambda **kw:
        pytest.fail("A-share portfolio cannot fall back to the single-stock optimizer"))
    monkeypatch.setattr(allocation, "enabled", lambda *a: True)
    def preview(symbol, *, account, **kw):
        return {"model": MODEL_KEY, "account": account, "plan_revision": "trusted",
            "verdict": "SELL" if account == "real" else "HOLD", "units": 100 if account == "real" else 0,
            "alloc_cny": -1000 if account == "real" else 0, "target_weight_pct": 0,
            "reason": "outside_top4_exit" if account == "real" else "weekly_plan_complete"}
    monkeypatch.setattr(allocation, "preview_active_decision", preview)
    req = server.CommitteeRequest(symbol="600001", current_price=10, cost=20, position_pct=10,
        cash=10000, shadow_cash=10000, optimizer_review_enabled=False,
        price_info={"price": 10},
        decision_mode="llm" if llm_mode else "algorithm_only",
        portfolio_decision={"model": MODEL_KEY, "verdict": "BUY", "units": 999999, "alloc_cny": 9999990})
    result = server.run_committee_direct(req)
    assert result.success, result.error
    assert result.verdict == "SELL"
    assert result.suggested_alloc_cny == -1000
    assert result.portfolio_optimizer["plan_revision"] == "trusted"
    assert result.shadow_result["verdict"] == "HOLD"
