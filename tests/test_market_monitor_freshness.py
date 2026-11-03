from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import MagicMock

import pytest

import jobs.market_monitor_runtime as runtime
import services.ashare_allocation as allocation
import utils.market_data_provider as provider
from core.transaction_cost_portfolio import MODEL_KEY
from jobs.market_monitor_news import MonitorNewsCache


NOW = datetime.fromisoformat("2026-10-30T10:00:00+08:00")


def quote(*, price=10, age=0, **fields):
    return {"price": price, "change_pct": 0,
            "fetched_at": (NOW - timedelta(seconds=age)).isoformat(), **fields}


@pytest.mark.parametrize("data,stale", [
    (quote(), False), (quote(age=180), False), (quote(age=181), True),
    (quote(age=-61), True), (quote(is_stale=True), True),
    (quote(price=float("nan")), True), (quote(price=0), True),
    (quote(fetched_at="invalid"), True), ({"price": 10}, True),
])
def test_quote_age_uses_original_timestamp(monkeypatch, data, stale):
    monkeypatch.setenv("INVEST_INTRADAY_QUOTE_MAX_AGE_SECONDS", "180")
    assert provider.quote_is_stale(data, now=NOW) is stale


def test_slow_news_does_not_block_or_start_duplicate_workers():
    started, release = Event(), Event()

    def fetch():
        started.set()
        assert release.wait(2)
        return ["news"]

    cache = MonitorNewsCache(fetch)
    try:
        assert cache.get() == []
        assert started.wait(1)
        worker = cache._thread
        assert cache.get() == []
        assert cache._thread is worker
    finally:
        release.set()
        cache._thread.join(2)
    assert cache.get() == ["news"]
    assert cache.get(enabled=False) == []


def test_news_cache_failure_does_not_discard_recent_completed_batch():
    cache = MonitorNewsCache(lambda: ["news"])
    cache.get()
    cache._thread.join(2)
    cache._updated_at -= 601
    cache._fetcher = MagicMock(side_effect=RuntimeError("offline"))
    assert cache.get() == ["news"]
    cache._thread.join(2)
    assert cache._items == ["news"]
    assert cache.get(enabled=False) == []


def test_disabled_news_never_starts_a_worker():
    fetch = MagicMock()
    cache = MonitorNewsCache(fetch)
    assert cache.get(enabled=False) == []
    assert cache._thread is None
    fetch.assert_not_called()


@pytest.mark.parametrize("refreshed", [{}, {"600001": quote(age=181)}, {"600001": {"price": 12}}])
def test_failed_quote_refresh_stays_blocked(monkeypatch, refreshed):
    monkeypatch.setattr(provider, "datetime", FrozenClock)
    monkeypatch.setattr(runtime, "fetch_sina_prices", lambda symbols: refreshed)
    prices = {"600001": quote(age=300)}
    runtime._refresh_stale_prices(prices, ["600001"])
    assert prices["600001"]["is_stale"]
    assert prices["600001"]["fetched_at"] == quote(age=300)["fetched_at"]


class FrozenClock(datetime):
    moment = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.moment.astimezone(tz) if tz else cls.moment.replace(tzinfo=None)


def make_plan(symbols=("600001",), remaining=-100):
    return {"model": MODEL_KEY, "account": "real", "revision": "trusted",
            "week": [2026, 44], "effective_at": "2026-10-26T09:30:00+08:00",
            "orders": {s: {"remaining_units": remaining, "reason": "outside_top4_exit",
                           "selected": False, "target_weight_pct": 0} for s in symbols}}


def waiting_result(symbol="600001"):
    return {"symbol": symbol, "name": "test", "success": True, "verdict": "HOLD",
            "confidence": .5, "suggested_alloc_cny": 0,
            "portfolio_optimizer": {"model": MODEL_KEY, "account": "real",
                "plan_revision": "trusted", "verdict": "HOLD", "units": 0,
                "alloc_cny": 0, "reason": "stale_quote"}}


@pytest.mark.parametrize("change_pct,trades,expected", [
    (0, [], "SELL"), (-10, [], "HOLD"),
    (0, [{"id": 1, "symbol": "600001", "direction": "BUY", "units": 100,
          "trade_date": "2026-10-30"}], "HOLD"),
])
def test_quote_refresh_restores_only_executable_exits(monkeypatch, change_pct, trades, expected):
    plan = make_plan()
    plan["trade_cursor"] = len(trades)
    monkeypatch.setattr(allocation, "preview_active_decision", lambda symbol, **kw:
        allocation.plan_decision(plan, symbol=symbol, price_info=kw["price_info"],
            holdings=[{"symbol": symbol, "units": 100}], trades=trades, cash=0, now=NOW))
    result = waiting_result()
    runtime._recheck_portfolio_result(result, price_info=quote(price=12, change_pct=change_pct),
                                      ledger=object(), is_holding=True)
    assert result["verdict"] == expected
    assert result["suggested_alloc_cny"] == (-1200 if expected == "SELL" else 0)
    assert result["decision_synthesis"]["final_action"] == expected


def test_recheck_cannot_bypass_entry_gate_or_change_plan_revision(monkeypatch):
    decision = {"model": MODEL_KEY, "plan_revision": "trusted", "verdict": "BUY",
                "units": 100, "alloc_cny": 1000, "reason": "cost_regularized_rebalance"}
    monkeypatch.setattr(allocation, "preview_active_decision", lambda *a, **kw: dict(decision))
    result = waiting_result()
    result["right_side_trend_gate"] = {"allow": False}
    runtime._recheck_portfolio_result(result, price_info=quote(), ledger=object(), is_holding=False)
    assert result["verdict"] == "HOLD"
    assert result["suggested_alloc_cny"] == 0
    decision.update(plan_revision="new", verdict="SELL", alloc_cny=-1000)
    runtime._recheck_portfolio_result(result, price_info=quote(), ledger=object(), is_holding=False)
    assert result["verdict"] == "WAIT"
    assert result["portfolio_optimizer"]["units"] == 0


@pytest.mark.parametrize("refresh_ok", [True, False])
def test_slow_monitor_round_rechecks_exits_before_publishing(monkeypatch, refresh_ok):
    import db.account_ledger as accounts
    import jobs.market_monitor_common as common
    import jobs.market_price_sentinel as sentinel

    FrozenClock.moment = NOW
    monkeypatch.setattr(runtime, "datetime", FrozenClock)
    monkeypatch.setattr(provider, "datetime", FrozenClock)
    stocks = [{"symbol": s, "name": "test", "market": "a", "units": 100,
               "position_pct": 10, "cost": 20} for s in ("600001", "600002")]
    plan = make_plan(tuple(s["symbol"] for s in stocks))
    ledger = MagicMock()
    ledger.stocks_for_committee_input.side_effect = lambda config, account: stocks if account == "real" else []
    ledger.list_holdings.side_effect = lambda account: stocks if account == "real" else []
    ledger.list_trades.return_value = []
    ledger.account_summary.return_value = {"cash_cny": 1000}
    ledger.snapshot_daily_pnl.return_value = []
    ledger.new_analysis_id.side_effect = lambda symbol, source: "test:" + symbol
    monkeypatch.setattr(accounts, "AccountLedger", lambda: ledger)
    config = {"holdings": stocks, "watchlist": [], "cash": 1000, "total_assets": 10000,
              "monitor_action_email_enabled": False}
    monkeypatch.setattr(runtime, "load_config", lambda: config)
    monkeypatch.setattr(common, "load_crawler_settings", lambda: {"news_refresh_enabled": False})
    monkeypatch.setattr(runtime, "load_sector_cache_mapping", lambda: {})
    monkeypatch.setattr(runtime, "load_entry_exit_alert_state", lambda: {})
    monkeypatch.setattr(runtime, "build_behavioral_factor_context", lambda stocks: {})
    monkeypatch.setattr(runtime, "build_hk_spatio_factor_context", lambda stocks: {})
    monkeypatch.setattr(runtime, "update_entry_exit_alert_state", lambda **kw: ([], []))
    monkeypatch.setattr(runtime, "write_report", lambda *a, **kw: None)
    monkeypatch.setattr(runtime, "MONITOR_POPUPS_ENABLED", False)
    monkeypatch.setattr(runtime.time, "sleep", lambda seconds: None)
    sentinel_type = MagicMock()
    sentinel_type.return_value.evaluate_round.return_value = {}
    monkeypatch.setattr(sentinel, "IntradayPriceSentinel", sentinel_type)
    monkeypatch.setattr(allocation, "enabled", lambda *a: True)
    monkeypatch.setattr(allocation, "prepare_account_plan", lambda **kw: None)

    def preview(symbol, **kw):
        return allocation.plan_decision(plan, symbol=symbol, price_info=kw["price_info"],
            holdings=stocks, trades=[], cash=1000, now=FrozenClock.moment)

    monkeypatch.setattr(allocation, "preview_active_decision", preview)
    fetches = []

    def fetch(symbols):
        fetches.append(list(symbols))
        if not refresh_ok and len(fetches) > 1:
            raise ConnectionError("offline")
        return {s: {"price": 10 + len(fetches), "change_pct": 0,
                    "fetched_at": FrozenClock.moment.astimezone(timezone.utc).isoformat()} for s in symbols}

    monkeypatch.setattr(runtime, "fetch_sina_prices", fetch)

    def slow_analysis(**kw):
        FrozenClock.moment += timedelta(minutes=4)
        result = waiting_result(kw["symbol"])
        result["portfolio_optimizer"] = preview(kw["symbol"], price_info=kw["price_info"])
        return result

    monkeypatch.setattr(runtime, "call_committee", slow_analysis)
    published = []
    monkeypatch.setattr(runtime, "write_monitor_window_snapshot", published.append)
    try:
        runtime.run_monitor_round()
    finally:
        FrozenClock.moment = NOW
    snapshot = published[0]
    assert snapshot["counts"]["action_required"] == (2 if refresh_ok else 0)
    if refresh_ok:
        assert [r["symbol"] for r in snapshot["actionable"]] == ["600001", "600002"]
        for row in snapshot["rows"]:
            assert row["operation"]["verdict"] == "SELL"
            assert row["portfolio_optimizer"]["reason"] == "outside_top4_exit"
            assert row["operation"]["suggested_alloc_cny"] == -100 * row["price"]["current"]
        assert fetches[-1] == ["600001"]
    else:
        for row in snapshot["rows"]:
            assert row["operation"]["verdict"] == "HOLD"
            assert row["portfolio_optimizer"]["reason"] == "stale_quote"
    assert all(o["remaining_units"] == -100 for o in plan["orders"].values())
    assert ledger.record_decision.call_count == 2
    ledger.apply_committee_result.assert_not_called()
