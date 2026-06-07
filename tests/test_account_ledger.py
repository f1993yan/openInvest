from __future__ import annotations

import pytest

from db.account_ledger import AccountLedger


def _config():
    return {
        "total_assets": 100000,
        "cash": 10000,
        "holdings": [
            {
                "symbol": "600900",
                "name": "长江电力",
                "market": "a",
                "sector": "公用事业",
                "industry": "水电运营",
                "position_pct": 20,
                "cost": 20,
            }
        ],
        "watchlist": [
            {
                "symbol": "000063",
                "name": "中兴通讯",
                "market": "a",
                "sector": "通信",
                "industry": "通信设备",
                "position_pct": 0,
                "cost": 30,
            }
        ],
    }


def test_initialize_dual_accounts_from_monitor_config(tmp_path):
    db = AccountLedger(str(tmp_path / "accounts.db"))
    assert db.initialize_from_monitor_config(_config()) is True
    assert db.initialize_from_monitor_config(_config()) is False

    real = {h["symbol"]: h for h in db.list_holdings("real")}
    shadow = {h["symbol"]: h for h in db.list_holdings("committee")}

    assert real["600900"]["units"] == pytest.approx(1000)
    assert shadow["600900"]["units"] == pytest.approx(1000)
    assert real["000063"]["units"] == 0
    assert db.account_summary("real")["cash_cny"] == pytest.approx(10000)


def test_real_account_only_changes_on_explicit_user_trade(tmp_path):
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_config())

    db.apply_committee_result(
        {
            "success": True,
            "symbol": "000063",
            "verdict": "BUY",
            "confidence": 0.7,
            "suggested_alloc_cny": 6000,
        },
        price=30,
    )
    real = {h["symbol"]: h for h in db.list_holdings("real")}
    shadow = {h["symbol"]: h for h in db.list_holdings("committee")}
    assert real["000063"]["units"] == 0
    assert shadow["000063"]["units"] == pytest.approx(200)

    db.apply_user_trade(symbol="000063", direction="BUY", units=100, price=30)
    real = {h["symbol"]: h for h in db.list_holdings("real")}
    assert real["000063"]["units"] == pytest.approx(100)


def test_committee_sell_is_limited_by_actual_shadow_holding(tmp_path):
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_config())

    trade = db.apply_committee_result(
        {
            "success": True,
            "symbol": "600900",
            "verdict": "SELL",
            "confidence": 0.8,
            "suggested_alloc_cny": -999999,
        },
        price=25,
    )
    assert trade is not None
    assert trade.units == pytest.approx(1000)
    shadow = {h["symbol"]: h for h in db.list_holdings("committee")}
    assert shadow["600900"]["units"] == 0


def test_daily_pnl_records_both_accounts(tmp_path):
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_config())
    rows = db.snapshot_daily_pnl(
        prices={"600900": 22, "000063": 30},
        trade_date="2026-06-06",
    )
    assert {r["account"] for r in rows} == {"real", "committee"}
    real = next(r for r in rows if r["account"] == "real")
    assert real["total_value_cny"] == pytest.approx(32000)
    assert real["total_pnl_cny"] == pytest.approx(-68000)

    hist = db.list_daily_pnl(limit=10)
    assert len(hist) == 2
