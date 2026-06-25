"""Dual stock-account ledger for committee reliability evaluation.

Two accounts are tracked:
- real: only changes when the user explicitly records executed trades.
- committee: automatically follows committee actions, constrained by cash,
  available holdings, and board-lot rules.

The initial positions are derived from jobs/market_monitor_config.json:
units ~= total_assets * position_pct / avg_cost. The baseline equity
(initial_equity_cny) is the actually-seeded day-0 value (cash + holdings at
cost), NOT total_assets — see _seed_equity.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "accounts.db")

REAL_ACCOUNT = "real"
COMMITTEE_ACCOUNT = "committee"
VALID_ACCOUNTS = {REAL_ACCOUNT, COMMITTEE_ACCOUNT}


@dataclass
class ExecutedTrade:
    account: str
    symbol: str
    direction: str
    units: float
    price: float
    cash_delta: float
    source: str
    note: str = ""


class AccountLedger:
    def __init__(self, db_path: Optional[str] = None) -> None:
        path = db_path or DB_PATH
        self.db_path = os.path.abspath(path)
        self._external_sync_enabled = self.db_path == os.path.abspath(DB_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        self.conn.commit()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account TEXT PRIMARY KEY,
                    cash_cny REAL NOT NULL,
                    initial_equity_cny REAL NOT NULL,
                    initialized_from TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS holdings (
                    account TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    market TEXT,
                    sector TEXT,
                    industry TEXT,
                    units REAL NOT NULL,
                    avg_cost REAL NOT NULL,
                    cost_currency TEXT NOT NULL DEFAULT 'CNY',
                    min_lot_size INTEGER NOT NULL DEFAULT 100,
                    PRIMARY KEY (account, symbol)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    account TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    units REAL NOT NULL,
                    price REAL NOT NULL,
                    cash_delta REAL NOT NULL,
                    source TEXT NOT NULL,
                    verdict TEXT,
                    confidence REAL,
                    note TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cash_settlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    amount_cny REAL NOT NULL,
                    trade_date TEXT NOT NULL,
                    settle_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    settled_at TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_pnl (
                    trade_date TEXT NOT NULL,
                    account TEXT NOT NULL,
                    total_value_cny REAL NOT NULL,
                    cash_cny REAL NOT NULL,
                    t2_pending_cash_cny REAL NOT NULL DEFAULT 0,
                    holdings_value_cny REAL NOT NULL,
                    cost_basis_cny REAL NOT NULL,
                    day_pnl_cny REAL,
                    total_pnl_cny REAL NOT NULL,
                    total_pnl_pct REAL NOT NULL,
                    prices_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (trade_date, account)
                )
            """)
            _ensure_column(cur, "daily_pnl", "t2_pending_cash_cny", "REAL NOT NULL DEFAULT 0")
            self._repair_legacy_unsettled_hk_sells(cur)
            self.conn.commit()

    def initialize_from_monitor_config(self, config: Dict[str, Any], *, reset: bool = False) -> bool:
        """Seed both accounts from current monitor config.

        Returns True when initialization writes data, False when existing state
        is kept.
        """
        with self._lock:
            if not reset and self._has_accounts():
                return False
            cur = self.conn.cursor()
            cur.execute("DELETE FROM daily_pnl")
            cur.execute("DELETE FROM trades")
            cur.execute("DELETE FROM cash_settlements")
            cur.execute("DELETE FROM holdings")
            cur.execute("DELETE FROM accounts")

            cash = float(config.get("cash", 0) or 0)
            total_assets = float(config.get("total_assets", 0) or 0)
            # 基线权益 = 实际播种的 day-0 总值（现金 + 持仓成本市值），
            # 不是 total_assets。total_assets 是"目标可投资规模"，而播种持仓
            # 往往只占其中一部分，剩余未投资部分并不等于 cash。若用 total_assets
            # 当基线，零交易首日就会算出 total_pnl = 播种值 - total_assets 的
            # 巨额假亏（example config：30000 - 100000 = -70%）。
            initial_equity = _seed_equity(config)
            now = _now()
            source = "jobs/market_monitor_config.json"
            for account in (REAL_ACCOUNT, COMMITTEE_ACCOUNT):
                cur.execute(
                    """INSERT INTO accounts
                       (account, cash_cny, initial_equity_cny, initialized_from, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (account, cash, initial_equity, source, now, now),
                )
                for stock in list(config.get("holdings", []) or []) + list(config.get("watchlist", []) or []):
                    avg_cost = float(stock.get("cost", 0) or 0)
                    units = _seed_units(stock, total_assets)
                    cur.execute(
                        """INSERT INTO holdings
                           (account, symbol, name, market, sector, industry, units, avg_cost,
                            cost_currency, min_lot_size)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CNY', ?)""",
                        (
                            account,
                            stock.get("symbol", ""),
                            stock.get("name", ""),
                            stock.get("market", "a"),
                            stock.get("sector", ""),
                            stock.get("industry", ""),
                            units,
                            avg_cost,
                            _min_lot_size(stock),
                        ),
                    )
            self.conn.commit()
            self.sync_to_config_and_snapshot()
            return True

    def ensure_initialized(self, config: Dict[str, Any]) -> None:
        self.initialize_from_monitor_config(config, reset=False)
        if _sync_real_from_config_enabled(config):
            self.sync_real_from_monitor_config(config)

    def sync_real_from_monitor_config(self, config: Dict[str, Any]) -> None:
        """Refresh real-account **metadata** from the monitor config.

        Real account 的可信源是用户真实成交（apply_user_trade，source=user_explicit），
        **不是 config**。所以本函数只同步元数据（name/market/sector/industry/min_lot_size）
        并为 config 新增、real 账户里尚不存在的标的做首次播种；对**已存在**的持仓，
        units/avg_cost 一律保留（由成交决定），cash 也不再每轮覆盖。

        旧实现每轮用 config 反推值整体覆盖 units/avg_cost/cash，会把用户刚记的真实
        成交在下一轮监控里抹平 —— holdings/cash 与 trades 历史自相矛盾，且 day_pnl
        把这种纯账务覆盖当成当日盈亏。docstring 担心的"已建仓位被 optimizer 当成
        underweight 机械补仓"本质是元数据/新标的缺失，靠"新标的首次播种 + 元数据
        刷新"即可解决，不需要覆盖已有持仓状态。
        """
        total_assets = float(config.get("total_assets", 0) or 0)
        now = _now()
        stocks = list(config.get("holdings", []) or []) + list(config.get("watchlist", []) or [])
        with self._lock:
            existing = {
                row["symbol"]
                for row in self.conn.execute(
                    "SELECT symbol FROM holdings WHERE account = ?", (REAL_ACCOUNT,)
                ).fetchall()
            }
            for stock in stocks:
                symbol = str(stock.get("symbol", "")).strip()
                if not symbol:
                    continue
                if symbol in existing:
                    # 已存在：只刷新元数据，保留成交驱动的 units / avg_cost。
                    self.conn.execute(
                        """UPDATE holdings SET name = ?, market = ?, sector = ?,
                             industry = ?, min_lot_size = ?
                           WHERE account = ? AND symbol = ?""",
                        (
                            stock.get("name", symbol),
                            stock.get("market", "a"),
                            stock.get("sector", ""),
                            stock.get("industry", ""),
                            _min_lot_size(stock),
                            REAL_ACCOUNT,
                            symbol,
                        ),
                    )
                    continue
                # 新标的：按 config 首次播种（此后由成交接管）。
                avg_cost = float(stock.get("cost", 0) or 0)
                units = _seed_units(stock, total_assets)
                self.conn.execute(
                    """INSERT INTO holdings
                       (account, symbol, name, market, sector, industry, units, avg_cost,
                        cost_currency, min_lot_size)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CNY', ?)""",
                    (
                        REAL_ACCOUNT,
                        symbol,
                        stock.get("name", symbol),
                        stock.get("market", "a"),
                        stock.get("sector", ""),
                        stock.get("industry", ""),
                        units,
                        avg_cost,
                        _min_lot_size(stock),
                    ),
                )
            self.conn.commit()

    def list_holdings(self, account: str) -> List[Dict[str, Any]]:
        _validate_account(account)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM holdings WHERE account = ? ORDER BY symbol",
                (account,),
            ).fetchall()
        return [dict(r) for r in rows]

    def account_summary(self, account: str) -> Dict[str, Any]:
        _validate_account(account)
        with self._lock:
            self.settle_due_cash(account=account)
            row = self.conn.execute("SELECT * FROM accounts WHERE account = ?", (account,)).fetchone()
            if not row:
                return {}
            out = dict(row)
            pending = self._pending_cash(account)
            out["available_cash_cny"] = float(out.get("cash_cny", 0) or 0)
            out["t2_pending_cash_cny"] = pending
            out["total_cash_cny"] = out["available_cash_cny"] + pending
            return out

    def settle_due_cash(self, *, account: Optional[str] = None, today: Optional[str] = None) -> float:
        """Release due HK T+2 settlement cash into available cash.

        ``accounts.cash_cny`` is the only cash allowed for new orders. Pending
        settlement remains in ``cash_settlements`` until ``settle_date``.
        """
        if account is not None:
            _validate_account(account)
        today = today or date.today().isoformat()
        with self._lock:
            params: List[Any] = [today]
            where_account = ""
            if account is not None:
                where_account = " AND account = ?"
                params.append(account)
            rows = self.conn.execute(
                f"""SELECT id, account, amount_cny FROM cash_settlements
                    WHERE status = 'pending' AND settle_date <= ?{where_account}""",
                params,
            ).fetchall()
            if not rows:
                return 0.0
            now = _now()
            released = 0.0
            for row in rows:
                amount = float(row["amount_cny"] or 0)
                if amount <= 0:
                    continue
                released += amount
                self.conn.execute(
                    "UPDATE accounts SET cash_cny = cash_cny + ?, updated_at = ? WHERE account = ?",
                    (amount, now, row["account"]),
                )
                self.conn.execute(
                    "UPDATE cash_settlements SET status = 'settled', settled_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
            self.conn.commit()
            if account == REAL_ACCOUNT or account is None:
                self.sync_to_config_and_snapshot()
            return released

    def stocks_for_committee_input(self, config: Dict[str, Any], account: str = REAL_ACCOUNT) -> List[Dict[str, Any]]:
        """Merge account units with config metadata for committee analysis."""
        _validate_account(account)
        holdings = {h["symbol"]: h for h in self.list_holdings(account)}
        # position_pct 的分母必须用 total_assets（总可投资规模），不能用
        # initial_equity_cny（= seed equity = cash + 持仓成本）。config 里用户写的
        # position_pct、以及 _seed_units 反推 units 用的都是 total_assets，三者同基准
        # 才能往返一致：_seed_units(pct=20%) → units，再 units*cost/total_assets → 20%。
        # 若用 initial_equity_cny（如 30000 vs total_assets 100000）会把集中度放大
        # 3.3×，委员会 Risk Officer 误判仓位爆表。fallback 到 seed equity 仅兜底。
        summary = self.account_summary(account)
        total_equity = float(config.get("total_assets") or summary.get("initial_equity_cny") or 0)
        out: List[Dict[str, Any]] = []
        for stock in list(config.get("holdings", []) or []) + list(config.get("watchlist", []) or []):
            h = holdings.get(stock.get("symbol", ""))
            merged = dict(stock)
            units = float((h or {}).get("units", 0) or 0)
            avg_cost = float((h or {}).get("avg_cost", stock.get("cost", 0)) or 0)
            # 按成本口径算占比（与 total_assets 的成本口径一致），非现价市值。
            cost_value = units * avg_cost
            merged["units"] = units
            merged["cost"] = avg_cost
            merged["position_pct"] = (cost_value / total_equity * 100.0) if total_equity > 0 else 0.0
            out.append(merged)
        return out

    def apply_user_trade(
        self,
        *,
        symbol: str,
        direction: str,
        units: float,
        price: float,
        trade_date: Optional[str] = None,
        note: str = "",
    ) -> ExecutedTrade:
        """Record a user-confirmed real-account trade."""
        return self._apply_trade(
            account=REAL_ACCOUNT,
            symbol=symbol,
            direction=direction,
            units=units,
            price=price,
            trade_date=trade_date,
            source="user_explicit",
            note=note,
            strict=True,
        )

    def apply_committee_result(
        self,
        result: Dict[str, Any],
        *,
        price: float,
        trade_date: Optional[str] = None,
    ) -> Optional[ExecutedTrade]:
        """Apply committee recommendation to the shadow account if executable."""
        if not result.get("success"):
            return None
        verdict = str(result.get("verdict", "")).upper()
        alloc = float(result.get("suggested_alloc_cny", 0) or 0)
        if price <= 0 or abs(alloc) <= 0:
            return None
        if verdict in {"BUY", "ACCUMULATE"}:
            direction = "BUY"
        elif verdict in {"TRIM", "SELL"}:
            direction = "SELL"
        else:
            return None
        symbol = str(result.get("symbol", "")).strip()
        holding = self._get_holding(COMMITTEE_ACCOUNT, symbol)
        lot = int((holding or {}).get("min_lot_size") or 100)
        raw_units = abs(alloc) / price
        units = _round_down_to_lot(raw_units, lot)
        if units <= 0:
            return None
        if direction == "BUY":
            cash = float(self.account_summary(COMMITTEE_ACCOUNT).get("cash_cny", 0) or 0)
            units = _round_down_to_lot(min(units, cash / price), lot)
        else:
            held = float((holding or {}).get("units", 0) or 0)
            units = _round_down_to_lot(min(units, held), lot)
        if units <= 0:
            return None
        return self._apply_trade(
            account=COMMITTEE_ACCOUNT,
            symbol=symbol,
            direction=direction,
            units=units,
            price=price,
            trade_date=trade_date,
            source="committee_auto",
            verdict=verdict,
            confidence=float(result.get("confidence", 0) or 0),
            note=f"alloc_cny={alloc:.2f}",
            strict=False,
        )

    def snapshot_daily_pnl(
        self,
        *,
        prices: Dict[str, float],
        trade_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        trade_date = trade_date or date.today().isoformat()
        rows: List[Dict[str, Any]] = []
        with self._lock:
            for account in (REAL_ACCOUNT, COMMITTEE_ACCOUNT):
                summary = self.account_summary(account)
                cash = float(summary.get("cash_cny", 0) or 0)
                pending_cash = float(summary.get("t2_pending_cash_cny", 0) or 0)
                initial_equity = float(summary.get("initial_equity_cny", 0) or 0)
                holdings = self.list_holdings(account)
                holdings_value = 0.0
                cost_basis = 0.0
                used_prices: Dict[str, float] = {}
                for h in holdings:
                    units = float(h.get("units", 0) or 0)
                    if units <= 0:
                        continue
                    symbol = h["symbol"]
                    price = float(prices.get(symbol) or h.get("avg_cost") or 0)
                    holdings_value += units * price
                    cost_basis += units * float(h.get("avg_cost", 0) or 0)
                    used_prices[symbol] = price
                total_value = cash + pending_cash + holdings_value
                prev = self.conn.execute(
                    """SELECT total_value_cny FROM daily_pnl
                       WHERE account = ? AND trade_date < ?
                       ORDER BY trade_date DESC LIMIT 1""",
                    (account, trade_date),
                ).fetchone()
                day_pnl = None if prev is None else total_value - float(prev["total_value_cny"])
                total_pnl = total_value - initial_equity
                total_pnl_pct = (total_pnl / initial_equity * 100.0) if initial_equity > 0 else 0.0
                now = _now()
                self.conn.execute(
                    """INSERT OR REPLACE INTO daily_pnl
                       (trade_date, account, total_value_cny, cash_cny, holdings_value_cny,
                        t2_pending_cash_cny, cost_basis_cny, day_pnl_cny, total_pnl_cny, total_pnl_pct,
                        prices_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        trade_date, account, total_value, cash, holdings_value, pending_cash,
                        cost_basis, day_pnl, total_pnl, total_pnl_pct,
                        json.dumps(used_prices, ensure_ascii=False, sort_keys=True), now,
                    ),
                )
                rows.append({
                    "trade_date": trade_date,
                    "account": account,
                    "total_value_cny": round(total_value, 2),
                    "cash_cny": round(cash, 2),
                    "available_cash_cny": round(cash, 2),
                    "t2_pending_cash_cny": round(pending_cash, 2),
                    "total_cash_cny": round(cash + pending_cash, 2),
                    "holdings_value_cny": round(holdings_value, 2),
                    "day_pnl_cny": None if day_pnl is None else round(day_pnl, 2),
                    "total_pnl_cny": round(total_pnl, 2),
                    "total_pnl_pct": round(total_pnl_pct, 4),
                })
            self.conn.commit()
        return rows

    def list_daily_pnl(self, account: Optional[str] = None, limit: int = 60) -> List[Dict[str, Any]]:
        params: List[Any] = []
        where = ""
        if account:
            _validate_account(account)
            where = "WHERE account = ?"
            params.append(account)
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM daily_pnl {where} ORDER BY trade_date DESC, account LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def list_trades(self, account: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        params: List[Any] = []
        where = ""
        if account:
            _validate_account(account)
            where = "WHERE account = ?"
            params.append(account)
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM trades {where} ORDER BY ts DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def _apply_trade(
        self,
        *,
        account: str,
        symbol: str,
        direction: str,
        units: float,
        price: float,
        trade_date: Optional[str],
        source: str,
        verdict: Optional[str] = None,
        confidence: Optional[float] = None,
        note: str = "",
        strict: bool,
    ) -> ExecutedTrade:
        _validate_account(account)
        direction = direction.upper()
        if direction not in {"BUY", "SELL"}:
            raise ValueError("direction must be BUY or SELL")
        if units <= 0 or price <= 0:
            raise ValueError("units and price must be positive")
        trade_date = trade_date or date.today().isoformat()
        with self._lock:
            self.settle_due_cash(account=account)
            holding = self._get_holding(account, symbol)
            if holding is None:
                if direction == "SELL":
                    raise ValueError(f"{account} has no holding for {symbol}")
                holding = {
                    "account": account,
                    "symbol": symbol,
                    "name": symbol,
                    "market": "a",
                    "sector": "",
                    "industry": "",
                    "units": 0.0,
                    "avg_cost": price,
                    "cost_currency": "CNY",
                    "min_lot_size": 100,
                }
                self.conn.execute(
                    """INSERT INTO holdings
                       (account, symbol, name, market, sector, industry, units, avg_cost,
                        cost_currency, min_lot_size)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'CNY', 100)""",
                    (account, symbol, symbol, "a", "", "", price),
                )
            current_units = float(holding.get("units", 0) or 0)
            current_avg = float(holding.get("avg_cost", 0) or 0)
            market = str(holding.get("market") or "").strip().lower()
            account_row = self.conn.execute(
                "SELECT cash_cny FROM accounts WHERE account = ?",
                (account,),
            ).fetchone()
            cash = float(account_row["cash_cny"] if account_row else 0)
            if direction == "BUY":
                cost = units * price
                if cost > cash + 1e-6:
                    if strict:
                        raise ValueError(f"insufficient cash: need {cost:.2f}, have {cash:.2f}")
                    units = cash / price
                    cost = units * price
                new_units = current_units + units
                new_avg = ((current_avg * current_units) + cost) / new_units if new_units > 0 else price
                cash_delta = -cost
            else:
                if units > current_units + 1e-6:
                    if strict:
                        raise ValueError(f"insufficient holding: sell {units}, have {current_units}")
                    units = current_units
                new_units = max(0.0, current_units - units)
                new_avg = current_avg
                proceeds = units * price
                if _is_hk_market(symbol, market):
                    cash_delta = 0.0
                    settle_date = _settlement_date(trade_date, sessions=2, calendar_code="XHKG")
                    note = _append_note(note, f"hk_t2_pending_cny={proceeds:.2f};settle_date={settle_date}")
                    self.conn.execute(
                        """INSERT INTO cash_settlements
                           (account, symbol, amount_cny, trade_date, settle_date, source, status, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                        (account, symbol, proceeds, trade_date, settle_date, source, _now()),
                    )
                else:
                    cash_delta = proceeds
            new_cash = cash + cash_delta
            now = _now()
            self.conn.execute(
                "UPDATE accounts SET cash_cny = ?, updated_at = ? WHERE account = ?",
                (new_cash, now, account),
            )
            self.conn.execute(
                "UPDATE holdings SET units = ?, avg_cost = ? WHERE account = ? AND symbol = ?",
                (new_units, new_avg, account, symbol),
            )
            self.conn.execute(
                """INSERT INTO trades
                   (ts, trade_date, account, symbol, direction, units, price, cash_delta,
                    source, verdict, confidence, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, trade_date, account, symbol, direction, units, price, cash_delta,
                 source, verdict, confidence, note),
            )
            self.conn.commit()
            if account == REAL_ACCOUNT:
                self.sync_to_config_and_snapshot()
        return ExecutedTrade(
            account=account,
            symbol=symbol,
            direction=direction,
            units=units,
            price=price,
            cash_delta=cash_delta,
            source=source,
            note=note,
        )

    def _pending_cash(self, account: str) -> float:
        row = self.conn.execute(
            """SELECT COALESCE(SUM(amount_cny), 0) AS amount
               FROM cash_settlements
               WHERE account = ? AND status = 'pending'""",
            (account,),
        ).fetchone()
        return float(row["amount"] if row else 0)

    def _repair_legacy_unsettled_hk_sells(self, cur: sqlite3.Cursor) -> None:
        today = date.today().isoformat()
        rows = cur.execute(
            """SELECT id, account, symbol, trade_date, cash_delta, source
               FROM trades
               WHERE direction = 'SELL' AND cash_delta > 0""",
        ).fetchall()
        for row in rows:
            symbol = str(row["symbol"] or "")
            if not _is_hk_market(symbol, ""):
                continue
            amount = float(row["cash_delta"] or 0)
            settle_date = _settlement_date(str(row["trade_date"]), sessions=2, calendar_code="XHKG")
            if settle_date <= today:
                continue
            exists = cur.execute(
                """SELECT 1 FROM cash_settlements
                   WHERE account = ? AND symbol = ? AND trade_date = ?
                     AND ABS(amount_cny - ?) < 0.01
                   LIMIT 1""",
                (row["account"], symbol, row["trade_date"], amount),
            ).fetchone()
            if exists:
                continue
            now = _now()
            cur.execute(
                """INSERT INTO cash_settlements
                   (account, symbol, amount_cny, trade_date, settle_date, source, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    row["account"],
                    symbol,
                    amount,
                    row["trade_date"],
                    settle_date,
                    f"legacy_hk_t2_repair:{row['source'] or 'unknown'}",
                    now,
                ),
            )
            cur.execute(
                "UPDATE accounts SET cash_cny = cash_cny - ?, updated_at = ? WHERE account = ?",
                (amount, now, row["account"]),
            )

    def _get_holding(self, account: str, symbol: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM holdings WHERE account = ? AND symbol = ?",
            (account, symbol),
        ).fetchone()
        return dict(row) if row else None

    def _has_accounts(self) -> bool:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()
        return bool(row and row["n"] >= 2)

    def add_holding(
        self,
        account: str,
        symbol: str,
        name: str,
        market: str,
        sector: str,
        industry: str,
        units: float,
        cost: float,
        min_lot_size: int = 100,
    ) -> bool:
        _validate_account(account)
        symbol = symbol.strip().upper()
        with self._lock:
            existing = self.conn.execute(
                "SELECT 1 FROM holdings WHERE account = ? AND UPPER(symbol) = ?",
                (account, symbol),
            ).fetchone()
            if existing:
                return False
            self.conn.execute(
                """INSERT INTO holdings
                   (account, symbol, name, market, sector, industry, units, avg_cost,
                    cost_currency, min_lot_size)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CNY', ?)""",
                (account, symbol, name, market, sector, industry, units, cost, min_lot_size),
            )
            self.conn.commit()
            if account == REAL_ACCOUNT:
                self.sync_to_config_and_snapshot()
            return True

    def update_holding(
        self,
        account: str,
        symbol: str,
        units: float,
        cost: float,
        is_tracking_only: bool = False,
    ) -> bool:
        _validate_account(account)
        symbol = symbol.strip().upper()
        if is_tracking_only:
            units = 0.0
            cost = 0.0
        with self._lock:
            existing = self.conn.execute(
                "SELECT 1 FROM holdings WHERE account = ? AND UPPER(symbol) = ?",
                (account, symbol),
            ).fetchone()
            if not existing:
                return False
            self.conn.execute(
                "UPDATE holdings SET units = ?, avg_cost = ? WHERE account = ? AND UPPER(symbol) = ?",
                (units, cost, account, symbol),
            )
            self.conn.commit()
            if account == REAL_ACCOUNT:
                self.sync_to_config_and_snapshot()
            return True

    def delete_holding(self, account: str, symbol: str) -> bool:
        _validate_account(account)
        symbol = symbol.strip().upper()
        with self._lock:
            existing = self.conn.execute(
                "SELECT 1 FROM holdings WHERE account = ? AND UPPER(symbol) = ?",
                (account, symbol),
            ).fetchone()
            if not existing:
                return False
            self.conn.execute(
                "DELETE FROM holdings WHERE account = ? AND UPPER(symbol) = ?",
                (account, symbol),
            )
            self.conn.commit()
            if account == REAL_ACCOUNT:
                self.sync_to_config_and_snapshot()
            return True

    def correct_cash(self, account: str, cash: float, t2_pending: Optional[float] = None) -> None:
        """Directly correct available cash and optionally T+2 pending cash in the ledger."""
        _validate_account(account)
        with self._lock:
            now = _now()
            self.conn.execute(
                "UPDATE accounts SET cash_cny = ?, updated_at = ? WHERE account = ?",
                (cash, now, account),
            )
            if t2_pending is not None:
                self.conn.execute(
                    "DELETE FROM cash_settlements WHERE account = ? AND status = 'pending'",
                    (account,),
                )
                if t2_pending > 0:
                    today = date.today().isoformat()
                    settle_date = _settlement_date(today, sessions=2, calendar_code="XHKG")
                    self.conn.execute(
                        """INSERT INTO cash_settlements
                           (account, symbol, amount_cny, trade_date, settle_date, source, status, created_at)
                           VALUES (?, 'CASH_CORRECTION', ?, ?, ?, 'cash_correction', 'pending', ?)""",
                        (account, t2_pending, today, settle_date, now),
                    )
            self.conn.commit()
            if account == REAL_ACCOUNT:
                self.sync_to_config_and_snapshot()

    def sync_to_config_and_snapshot(self) -> None:
        """Sync real account cash, pending cash, holdings, and watchlist to config and snapshot."""
        if not self._external_sync_enabled:
            return
        # Paths
        project_root = Path(os.path.dirname(__file__)).parent
        config_path = project_root / "jobs" / "market_monitor_config.json"
        snapshot_path = project_root / "data" / "market_monitor" / "latest_window.json"

        if not config_path.exists():
            return

        with self._lock:
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                return

            total_assets = float(config.get("total_assets", 0.0) or 0.0)

            # Get cash from ledger
            summary = self.account_summary(REAL_ACCOUNT)
            config["cash"] = round(float(summary.get("available_cash_cny", 0.0) or 0.0), 2)
            config["t2_pending_cash"] = round(float(summary.get("t2_pending_cash_cny", 0.0) or 0.0), 2)

            # Create map of existing config details (holdings & watchlist) to preserve target_position_pct, sector_source, etc.
            existing_details = {}
            for s in list(config.get("holdings", []) or []) + list(config.get("watchlist", []) or []):
                sym = str(s.get("symbol")).strip().upper()
                if sym:
                    existing_details[sym] = s

            # Construct new holdings and watchlist lists from ledger
            new_holdings = []
            new_watchlist = []

            ledger_holdings = self.list_holdings(REAL_ACCOUNT)
            for h in ledger_holdings:
                sym = str(h["symbol"]).strip().upper()
                # Start with existing config item details if present to preserve other keys
                item = dict(existing_details.get(sym, {}))

                item["symbol"] = h["symbol"]
                item["name"] = h["name"] or item.get("name", h["symbol"])
                item["market"] = h["market"] or item.get("market", "a")
                item["sector"] = h["sector"] or item.get("sector", "")
                item["industry"] = h["industry"] or item.get("industry", "")
                item["min_lot_size"] = int(h["min_lot_size"] or 100)

                units = float(h["units"] or 0.0)
                cost = float(h["avg_cost"] or 0.0)

                if units > 0:
                    item["units"] = units
                    item["cost"] = cost
                    item["position_pct"] = round((units * cost) / total_assets * 100.0, 4) if total_assets > 0 else 0.0
                    new_holdings.append(item)
                else:
                    # watchlist item
                    item.pop("units", None)
                    item["cost"] = 0.0
                    item["position_pct"] = 0.0
                    new_watchlist.append(item)

            config["holdings"] = new_holdings
            config["watchlist"] = new_watchlist

            # Write config to disk
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

            # Update snapshot
            try:
                from scripts.monitor_window_services import _load_config_snapshot
                from jobs.market_monitor_snapshot import write_monitor_window_snapshot

                snapshot = _load_config_snapshot("实时同步账本数据", source_path=snapshot_path)
                write_monitor_window_snapshot(snapshot, snapshot_path)
            except Exception:
                # If it fails, delete snapshot so it's rebuilt on next load
                if snapshot_path.exists():
                    try:
                        snapshot_path.unlink()
                    except Exception:
                        pass


def _validate_account(account: str) -> None:
    if account not in VALID_ACCOUNTS:
        raise ValueError(f"account must be one of {sorted(VALID_ACCOUNTS)}, got {account!r}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_column(cur: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
    existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _is_hk_market(symbol: str, market: str) -> bool:
    market_text = str(market or "").strip().lower()
    symbol_text = str(symbol or "").strip().upper()
    return market_text in {"hk", "hkg", "hongkong"} or symbol_text.endswith(".HK") or (
        symbol_text.isdigit() and len(symbol_text) == 5
    )


def _settlement_date(trade_date: str, *, sessions: int, calendar_code: str) -> str:
    base = datetime.strptime(trade_date[:10], "%Y-%m-%d").date()
    try:
        import exchange_calendars as xcal  # type: ignore

        cal = xcal.get_calendar(calendar_code)
        current = base.isoformat()
        if not cal.is_session(current):
            current = cal.date_to_session(current, direction="next").date().isoformat()
        session = current
        for _ in range(max(0, sessions)):
            session = cal.next_session(session).date().isoformat()
        return session
    except Exception:
        current = base
        remaining = max(0, sessions)
        while remaining > 0:
            current += timedelta(days=1)
            if current.weekday() < 5:
                remaining -= 1
        return current.isoformat()


def _append_note(note: str, extra: str) -> str:
    note = str(note or "").strip()
    extra = str(extra or "").strip()
    if not note:
        return extra
    if not extra:
        return note
    return f"{note};{extra}"


def _min_lot_size(stock: Dict[str, Any]) -> int:
    if int(stock.get("min_lot_size") or 0) > 0:
        return int(stock["min_lot_size"])
    return 100


def _seed_units(stock: Dict[str, Any], total_assets: float) -> float:
    """从 config 的单条持仓推算播种股数（单一可信源）。

    两种来源，优先级 explicit units > position_pct：
    - 若 config 显式给了 ``units``，直接用（用户精确录入）。
    - 否则按 ``position_pct`` 反推：units = total_assets * pct% / avg_cost。

    返回 0 表示该条不构成实际持仓（缺 cost / pct=0 / 无 total_assets，
    典型是 watchlist 观察标的）。

    抽成 helper 是因为这个公式原本在 initialize_from_monitor_config 和
    sync_real_from_monitor_config 各写了一份，是跨入口漂移的典型隐患
    （见 CLAUDE.md 分层契约）。基线 initial_equity 也依赖它，必须单点可信。
    """
    if "units" in stock:
        return float(stock.get("units", 0) or 0)
    position_pct = float(stock.get("position_pct", 0) or 0)
    avg_cost = float(stock.get("cost", 0) or 0)
    if position_pct > 0 and avg_cost > 0 and total_assets > 0:
        return (total_assets * position_pct / 100.0) / avg_cost
    return 0.0


def _seed_equity(config: Dict[str, Any]) -> float:
    """播种时刻的真实账户权益 = 现金 + Σ(播种股数 × 成本价)。

    这是 day-0 的 total_value，必须作为 initial_equity_cny 的基线，
    否则零交易首日就会算出虚假盈亏（详见下方调用点注释）。
    """
    cash = float(config.get("cash", 0) or 0)
    total_assets = float(config.get("total_assets", 0) or 0)
    stocks = list(config.get("holdings", []) or []) + list(config.get("watchlist", []) or [])
    holdings_cost = 0.0
    for stock in stocks:
        units = _seed_units(stock, total_assets)
        avg_cost = float(stock.get("cost", 0) or 0)
        holdings_cost += units * avg_cost
    return cash + holdings_cost


def _sync_real_from_config_enabled(config: Dict[str, Any]) -> bool:
    env = os.getenv("INVEST_SYNC_REAL_FROM_CONFIG")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "no", "off"}
    return bool(config.get("sync_real_account_from_config", True))


def _round_down_to_lot(units: float, lot: int) -> float:
    if lot <= 1:
        return max(0.0, units)
    return float(int(units // lot) * lot)


def prices_from_sina_result(prices: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    return {
        symbol: float(info.get("price") or 0)
        for symbol, info in prices.items()
        if float(info.get("price") or 0) > 0
    }
