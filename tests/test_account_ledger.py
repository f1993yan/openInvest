from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from db.account_ledger import AccountLedger, _settlement_date


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


def _hk_config():
    config = _config()
    config["holdings"] = [
        {
            "symbol": "09988",
            "name": "阿里巴巴-W",
            "market": "hk",
            "sector": "互联网",
            "industry": "电商",
            "position_pct": 20,
            "cost": 80,
            "min_lot_size": 100,
        }
    ]
    config["watchlist"] = []
    return config


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


def test_sync_real_account_preserves_traded_holdings_refreshes_metadata(tmp_path):
    # 契约：real 账户以真实成交（apply_user_trade）为准。sync 只刷新元数据，
    # 不覆盖已存在持仓的 units/avg_cost，也不覆盖 cash。用户在 config 里改
    # position_pct/cost 不应影响已建仓位（旧实现会覆盖，会抹掉用户成交）。
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_config())

    updated = _config()
    updated["cash"] = 5000
    updated["total_assets"] = 120000
    updated["holdings"][0]["position_pct"] = 30
    updated["holdings"][0]["cost"] = 24
    # 同时改元数据，验证元数据会被刷新
    updated["holdings"][0]["sector"] = "电力新板块"
    db.sync_real_from_monitor_config(updated)

    real = {h["symbol"]: h for h in db.list_holdings("real")}
    # 已存在持仓：units/avg_cost 保持播种值，不被 config 覆盖
    assert real["600900"]["units"] == pytest.approx(1000)
    assert real["600900"]["avg_cost"] == pytest.approx(20)
    # 元数据被刷新
    assert real["600900"]["sector"] == "电力新板块"
    # cash 不被 config 每轮覆盖（由成交驱动）
    assert db.account_summary("real")["cash_cny"] == pytest.approx(10000)


def test_sync_real_account_seeds_new_config_symbol(tmp_path):
    # config 新增、real 账户尚不存在的标的，sync 会做首次播种。
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_config())

    updated = _config()
    updated["holdings"].append({
        "symbol": "601398", "name": "工商银行", "market": "a",
        "sector": "银行", "industry": "国有大行",
        "position_pct": 10, "cost": 5,
    })
    db.sync_real_from_monitor_config(updated)

    real = {h["symbol"]: h for h in db.list_holdings("real")}
    # 新标的首次播种：units = total_assets 100000 * 10% / 5 = 2000
    assert real["601398"]["units"] == pytest.approx(2000)
    assert real["601398"]["avg_cost"] == pytest.approx(5)


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
    # 基线 = 实际播种权益 = cash 10000 + 持仓成本 1000*20 = 30000（不是 total_assets
    # 100000）。快照价 22 时市值 32000，故 total_pnl = 32000 - 30000 = +2000。
    # 旧实现误用 total_assets 当基线，零交易首日就报 -68000 假亏（已修，见 _seed_equity）。
    assert real["total_pnl_cny"] == pytest.approx(2000)

    hist = db.list_daily_pnl(limit=10)
    assert len(hist) == 2


def test_hk_sell_proceeds_are_t2_pending_not_available_cash(tmp_path):
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_hk_config())

    trade = db.apply_user_trade(
        symbol="09988",
        direction="SELL",
        units=100,
        price=90,
        trade_date="2026-06-19",
    )
    assert trade.cash_delta == pytest.approx(0)

    summary = db.account_summary("real")
    assert summary["cash_cny"] == pytest.approx(10000)
    assert summary["available_cash_cny"] == pytest.approx(10000)
    assert summary["t2_pending_cash_cny"] == pytest.approx(9000)
    assert summary["total_cash_cny"] == pytest.approx(19000)

    rows = db.snapshot_daily_pnl(prices={"09988": 90}, trade_date="2026-06-19")
    real = next(r for r in rows if r["account"] == "real")
    assert real["cash_cny"] == pytest.approx(10000)
    assert real["t2_pending_cash_cny"] == pytest.approx(9000)
    assert real["total_value_cny"] == pytest.approx(32500)


def test_due_hk_t2_cash_is_released_to_available_cash(tmp_path):
    trade_date = "2026-06-19"
    settle_date = _settlement_date(trade_date, sessions=2, calendar_code="XHKG")
    before_settle_date = (datetime.strptime(settle_date, "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_hk_config())
    db.apply_user_trade(
        symbol="09988",
        direction="SELL",
        units=100,
        price=90,
        trade_date=trade_date,
    )

    assert db.settle_due_cash(account="real", today=before_settle_date) == pytest.approx(0)
    assert db.account_summary("real")["cash_cny"] == pytest.approx(10000)

    assert db.settle_due_cash(account="real", today=settle_date) == pytest.approx(9000)
    summary = db.account_summary("real")
    assert summary["cash_cny"] == pytest.approx(19000)
    assert summary["t2_pending_cash_cny"] == pytest.approx(0)
