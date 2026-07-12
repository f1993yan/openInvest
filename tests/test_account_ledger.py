from __future__ import annotations

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import json

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


def test_a_share_same_day_buy_units_are_not_sellable_for_real_account(tmp_path):
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_config())
    trade_date = "2026-07-01"

    db.apply_user_trade(symbol="000063", direction="BUY", units=200, price=30, trade_date=trade_date)

    with pytest.raises(ValueError, match="A股T\\+1限制"):
        db.apply_user_trade(symbol="000063", direction="SELL", units=100, price=29, trade_date=trade_date)

    real = {h["symbol"]: h for h in db.list_holdings("real")}
    assert real["000063"]["units"] == pytest.approx(200)


def test_a_share_t1_allows_selling_only_older_units(tmp_path):
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_config())
    trade_date = "2026-07-01"

    db.apply_user_trade(symbol="600900", direction="BUY", units=100, price=20, trade_date=trade_date)

    with pytest.raises(ValueError, match="今日可卖 1000 股"):
        db.apply_user_trade(symbol="600900", direction="SELL", units=1100, price=21, trade_date=trade_date)

    trade = db.apply_user_trade(symbol="600900", direction="SELL", units=1000, price=21, trade_date=trade_date)
    assert trade.units == pytest.approx(1000)
    real = {h["symbol"]: h for h in db.list_holdings("real")}
    assert real["600900"]["units"] == pytest.approx(100)


def test_committee_sell_caps_to_a_share_t1_sellable_units(tmp_path):
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_config())
    trade_date = "2026-07-01"

    db.apply_committee_result(
        {
            "success": True,
            "symbol": "600900",
            "verdict": "ACCUMULATE",
            "confidence": 0.8,
            "suggested_alloc_cny": 2000,
        },
        price=20,
        trade_date=trade_date,
    )

    trade = db.apply_committee_result(
        {
            "success": True,
            "symbol": "600900",
            "verdict": "SELL",
            "confidence": 0.8,
            "suggested_alloc_cny": -999999,
        },
        price=21,
        trade_date=trade_date,
    )

    assert trade is not None
    assert trade.units == pytest.approx(1000)
    shadow = {h["symbol"]: h for h in db.list_holdings("committee")}
    assert shadow["600900"]["units"] == pytest.approx(100)


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

    trade_date_str = datetime.today().date().isoformat()
    trade = db.apply_user_trade(
        symbol="09988",
        direction="SELL",
        units=100,
        price=90,
        trade_date=trade_date_str,
    )
    assert trade.cash_delta == pytest.approx(0)

    summary = db.account_summary("real")
    assert summary["cash_cny"] == pytest.approx(10000)
    assert summary["available_cash_cny"] == pytest.approx(10000)
    assert summary["t2_pending_cash_cny"] == pytest.approx(9000)
    assert summary["total_cash_cny"] == pytest.approx(19000)

    rows = db.snapshot_daily_pnl(prices={"09988": 90}, trade_date=trade_date_str)
    real = next(r for r in rows if r["account"] == "real")
    assert real["cash_cny"] == pytest.approx(10000)
    assert real["t2_pending_cash_cny"] == pytest.approx(9000)
    assert real["total_value_cny"] == pytest.approx(32500)


def test_due_hk_t2_cash_is_released_to_available_cash(tmp_path):
    trade_date = datetime.today().date().isoformat()
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


def test_temp_ledger_does_not_sync_project_config(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    config_dir = project_root / "jobs"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "market_monitor_config.json"
    original = _config()
    config_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    import db.account_ledger as account_ledger

    monkeypatch.setattr(account_ledger, "__file__", str(project_root / "db" / "account_ledger.py"))
    db = AccountLedger(str(tmp_path / "isolated_accounts.db"))
    db.initialize_from_monitor_config(_config())
    db.apply_user_trade(symbol="000063", direction="BUY", units=100, price=30)

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted == original


def test_sync_to_config_preserves_existing_actionable_snapshot_rows(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    config_dir = project_root / "jobs"
    snapshot_dir = project_root / "data" / "market_monitor"
    config_dir.mkdir(parents=True)
    snapshot_dir.mkdir(parents=True)
    config_path = config_dir / "market_monitor_config.json"
    snapshot_path = snapshot_dir / "latest_window.json"
    config_path.write_text(json.dumps(_config(), ensure_ascii=False), encoding="utf-8")
    snapshot_path.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-07-01T10:00:00",
                "round_time": "10:00",
                "counts": {"symbols": 2, "action_required": 2},
                "rows": [
                    {
                        "symbol": "600900",
                        "name": "长江电力",
                        "state": "action_required",
                        "units": 1000,
                        "operation": {"status": "action_required", "verdict": "SELL", "suggested_alloc_cny": -25000},
                    },
                    {
                        "symbol": "000063",
                        "name": "中兴通讯",
                        "state": "action_required",
                        "units": 0,
                        "operation": {"status": "action_required", "verdict": "BUY", "suggested_alloc_cny": 6000},
                    },
                ],
                "actionable": [{"symbol": "600900"}, {"symbol": "000063"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    import db.account_ledger as account_ledger

    monkeypatch.setattr(account_ledger, "__file__", str(project_root / "db" / "account_ledger.py"))
    db = AccountLedger(str(project_root / "db" / "accounts.db"))
    db.initialize_from_monitor_config(_config())
    db.apply_user_trade(symbol="600900", direction="SELL", units=100, price=21, trade_date="2026-07-02")

    synced = json.loads(snapshot_path.read_text(encoding="utf-8"))
    by_symbol = {row["symbol"]: row for row in synced["rows"]}
    assert set(by_symbol) == {"600900", "000063"}
    assert by_symbol["000063"]["state"] == "action_required"
    assert by_symbol["000063"]["operation"]["verdict"] == "BUY"
    assert synced["counts"]["action_required"] == 2
    assert synced["actionable"] == [{"symbol": "600900"}, {"symbol": "000063"}]


def test_trade_idempotency_replays_without_mutating_account_twice(tmp_path):
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_config())

    first = db.apply_user_trade(
        symbol="000063",
        direction="BUY",
        units=100,
        price=30,
        idempotency_key="ui-click-000063-buy-100-v1",
    )
    replay = db.apply_user_trade(
        symbol="000063",
        direction="BUY",
        units=100,
        price=30,
        idempotency_key="ui-click-000063-buy-100-v1",
    )

    assert replay.id == first.id
    assert db.account_summary("real")["cash_cny"] == pytest.approx(7000)
    assert {h["symbol"]: h for h in db.list_holdings("real")}["000063"]["units"] == 100
    assert len(db.list_trades("real")) == 1


def test_cross_connection_idempotency_is_transactional(tmp_path):
    path = tmp_path / "accounts.db"
    primary = AccountLedger(str(path))
    primary.initialize_from_monitor_config(_config())
    peer = AccountLedger(str(path))

    def execute(ledger):
        return ledger.apply_user_trade(
            symbol="000063",
            direction="BUY",
            units=100,
            price=30,
            idempotency_key="cross-process-click-000063-buy",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        trades = list(pool.map(execute, (primary, peer)))

    assert trades[0].id == trades[1].id
    assert len(primary.list_trades("real")) == 1
    assert primary.account_summary("real")["cash_cny"] == pytest.approx(7000)


def test_completed_shadow_decision_replays_original_trade(tmp_path):
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_config())
    result = {
        "success": True,
        "symbol": "000063",
        "verdict": "BUY",
        "confidence": 0.8,
        "suggested_alloc_cny": 9000,
    }
    db.record_decision(result, account="committee", analysis_id=db.new_analysis_id("000063", "test"))

    first = db.apply_committee_result(result, price=30)
    replay = db.apply_committee_result(result, price=30)

    assert first is not None and replay is not None
    assert replay.id == first.id
    assert len(db.list_trades("committee")) == 1
    assert {h["symbol"]: h for h in db.list_holdings("committee")}["000063"]["units"] == 300


def test_real_and_shadow_decisions_have_distinct_ids_and_outcomes(tmp_path):
    db = AccountLedger(str(tmp_path / "accounts.db"))
    db.initialize_from_monitor_config(_config())
    analysis_id = db.new_analysis_id("000063", "test")
    real = {"success": True, "symbol": "000063", "verdict": "BUY", "confidence": 0.7, "suggested_alloc_cny": 3000}
    shadow = {"success": True, "symbol": "000063", "verdict": "HOLD", "confidence": 0.6, "suggested_alloc_cny": 0}

    real_id = db.record_decision(real, account="real", analysis_id=analysis_id)
    shadow_id = db.record_decision(shadow, account="committee", analysis_id=analysis_id)
    assert real_id != shadow_id
    assert db.record_decision_outcome(real_id, horizon_days=30, return_pct=5.2, benchmark_return_pct=2.0, hit=True)
    assert db.record_decision_response(real_id, accepted=False, reason="user declined")
    assert not db.record_decision_response(shadow_id, accepted=False, reason="must stay private")

    real_rows = db.list_decisions(account="real")
    shadow_rows = db.list_decisions(account="committee")
    assert len(real_rows) == len(shadow_rows) == 1
    assert real_rows[0]["status"] == "rejected"
    assert real_rows[0]["outcomes"]["30d"]["excess_return_pct"] == pytest.approx(3.2)
    assert shadow_rows[0]["status"] == "proposed"


def test_schema_migration_adds_decision_columns(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                trade_date TEXT NOT NULL, account TEXT NOT NULL, symbol TEXT NOT NULL,
                direction TEXT NOT NULL, units REAL NOT NULL, price REAL NOT NULL,
                cash_delta REAL NOT NULL, source TEXT NOT NULL, verdict TEXT,
                confidence REAL, note TEXT
            )"""
        )

    db = AccountLedger(str(path))
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(trades)")}
    assert {"decision_id", "idempotency_key"} <= columns
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] >= 2
