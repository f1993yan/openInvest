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
from datetime import date, datetime, timezone
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
                CREATE TABLE IF NOT EXISTS daily_pnl (
                    trade_date TEXT NOT NULL,
                    account TEXT NOT NULL,
                    total_value_cny REAL NOT NULL,
                    cash_cny REAL NOT NULL,
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
            row = self.conn.execute("SELECT * FROM accounts WHERE account = ?", (account,)).fetchone()
        return dict(row) if row else {}

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
                total_value = cash + holdings_value
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
                        cost_basis_cny, day_pnl_cny, total_pnl_cny, total_pnl_pct,
                        prices_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        trade_date, account, total_value, cash, holdings_value,
                        cost_basis, day_pnl, total_pnl, total_pnl_pct,
                        json.dumps(used_prices, ensure_ascii=False, sort_keys=True), now,
                    ),
                )
                rows.append({
                    "trade_date": trade_date,
                    "account": account,
                    "total_value_cny": round(total_value, 2),
                    "cash_cny": round(cash, 2),
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
            cash = float(self.account_summary(account).get("cash_cny", 0) or 0)
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
                cash_delta = units * price
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

    def _get_holding(self, account: str, symbol: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM holdings WHERE account = ? AND symbol = ?",
            (account, symbol),
        ).fetchone()
        return dict(row) if row else None

    def _has_accounts(self) -> bool:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()
        return bool(row and row["n"] >= 2)


def _validate_account(account: str) -> None:
    if account not in VALID_ACCOUNTS:
        raise ValueError(f"account must be one of {sorted(VALID_ACCOUNTS)}, got {account!r}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
