"""Prepare production allocation plans and refresh the desktop without placing trades."""
from __future__ import annotations

import json
import hashlib
from datetime import datetime
from pathlib import Path

from core.transaction_cost_portfolio import MODEL_KEY
from services.ashare_allocation import STATE_PATH, TZ, is_ashare, prepare_account_plan, preview_active_decision
from utils.safe_persistence import atomic_write_json, create_backup_manifest

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    from db.account_ledger import AccountLedger
    from jobs.market_monitor_runtime import load_config
    from jobs.market_monitor_quotes import build_behavioral_factor_context, fetch_sina_prices

    from db.account_ledger import DB_PATH
    config_path = ROOT/"jobs/market_monitor_config.json"
    snapshot_path = ROOT/"data/market_monitor/latest_window.json"
    backup = create_backup_manifest([STATE_PATH, snapshot_path, config_path,
        ROOT/"data/behavioral_factor_state.json", Path(DB_PATH)], reason="activate-transaction-cost-portfolio")
    config = load_config()
    stocks = config.get("holdings", [])+config.get("watchlist", [])
    factors = build_behavioral_factor_context(stocks)
    plans = {}
    with AccountLedger() as ledger:
        def inventory_fingerprint():
            rows = [tuple(row) for row in ledger.conn.execute(
                "SELECT account, symbol, units, avg_cost FROM holdings ORDER BY account, symbol")]
            return hashlib.sha256(json.dumps(rows).encode()).hexdigest()
        before_inventory = inventory_fingerprint()
        before_trades = ledger.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        symbols = {s["symbol"] for s in stocks}
        for account in ("real", "committee"):
            symbols.update(h["symbol"] for h in ledger.list_holdings(account) if float(h.get("units", 0)) > 0)
        prices = fetch_sina_prices(sorted(symbols))
        for account in ("real", "committee"):
            summary = ledger.account_summary(account)
            plan = prepare_account_plan(account=account, holdings=ledger.list_holdings(account),
                trades=ledger.list_trades(account, limit=100000), cash=summary.get("cash_cny", 0),
                pending_cash=summary.get("t2_pending_cash_cny", 0), prices=prices, factors=factors,
                trading_mode=config.get("trading_mode", "active_profit"))
            plans[account] = {"model": plan["model"], "revision": plan["revision"],
                              "solver_status": plan["solver"]["status"], "effective_at": plan["effective_at"],
                              "created_at": plan["created_at"], "data_as_of": plan["data_as_of"]}
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["ashare_allocation_model"] = MODEL_KEY
        atomic_write_json(config_path, raw_config)
        if snapshot_path.exists():
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            planned = set()
            for row in snapshot.get("rows", []):
                symbol = str(row.get("symbol", ""))
                if not is_ashare(row):
                    continue
                planned.add(symbol)
                decision = preview_active_decision(symbol, account="real", price_info=prices.get(symbol, {}), ledger=ledger)
                row["portfolio_optimizer"] = decision
                row["operation"] = {**row.get("operation", {}), "verdict": "HOLD", "status": "monitoring",
                    "suggested_alloc_cny": 0, "execution_units": 0, "optimizer_lots": 0,
                    "portfolio_optimizer": decision, "reason": "portfolio_plan_activated"}
                row["state"] = "monitoring"
                row["decision_synthesis"] = {"final_action": "HOLD", "primary_reason": "组合计划已更新，等待下一轮监控确认交易条件",
                                             "evidence": [], "risk_warnings": [], "conflicts": []}
            snapshot["actionable"] = [r for r in snapshot.get("actionable", []) if r.get("symbol") not in planned]
            snapshot.setdefault("counts", {})["action_required"] = len(snapshot["actionable"])
            snapshot["allocation_model"] = MODEL_KEY
            snapshot["allocation_activated_at"] = datetime.now(TZ).isoformat(timespec="seconds")
            atomic_write_json(snapshot_path, snapshot)
        inventory_unchanged = before_inventory == inventory_fingerprint()
        trades_placed = ledger.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]-before_trades
        if not inventory_unchanged or trades_placed:
            raise RuntimeError("account_changed_during_activation")
    # Report only operational metadata, never securities, cash, positions or keys.
    print(json.dumps({"activated": True, "model": MODEL_KEY, "accounts": plans,
                      "backup_id": backup["backup_id"], "trades_placed": trades_placed,
                      "inventory_unchanged": inventory_unchanged, "notifications_sent": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
