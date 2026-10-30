"""Account-specific weekly plans; completed orders never refill after manual sales."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from core.transaction_cost_portfolio import (
    MODEL_KEY, PARAMETERS, historical_covariance, optimize_transaction_cost_portfolio,
)
from jobs.trading_mode import normalize_trading_mode, trading_mode_cash_reserve_ratio
from utils.safe_persistence import atomic_write_json

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "ashare_allocation_state.json"
TZ = ZoneInfo("Asia/Shanghai")
log = logging.getLogger(__name__)


def enabled(config: dict | None = None) -> bool:
    if config is None:
        try:
            config = json.loads((ROOT/"jobs/market_monitor_config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
    choice = (config or {}).get("ashare_allocation_model") or os.getenv(
        "INVEST_ASHARE_ALLOCATION_MODEL", MODEL_KEY)
    return str(choice).lower() not in {"legacy", "single_stock", "off", "disabled"}


def is_ashare(stock: dict) -> bool:
    return str(stock.get("market") or "a").lower() in {"a", "cn", "ashare"} and len(str(stock.get("symbol", ""))) == 6


def _number(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError):
        return default


def _local(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(TZ)
    return moment.replace(tzinfo=TZ) if moment.tzinfo is None else moment.astimezone(TZ)


def _clock(moment: datetime) -> tuple[str, str, bool]:
    import exchange_calendars as xcal

    calendar = xcal.get_calendar("XSHG")
    day = moment.date().isoformat()
    session = calendar.date_to_session(day, direction="previous")
    completed = session
    if session.date() == moment.date() and moment.time() < time(15, 0):
        completed = calendar.previous_session(session)
    effective = calendar.date_to_session(day, direction="next")
    if effective.date() == moment.date() and moment.time() > time(15, 0):
        effective = calendar.next_session(effective)
    open_now = (session.date() == moment.date()
                and (time(9, 30) <= moment.time() <= time(11, 30)
                     or time(13, 0) <= moment.time() <= time(15, 0)))
    return str(completed.date()), str(effective.date()), open_now


def _read(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "model": MODEL_KEY, "accounts": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("model") != MODEL_KEY or not isinstance(data.get("accounts"), dict):
        raise ValueError("invalid_allocation_state")
    return data


def _revision(factors: dict, session: str, mode: str) -> str:
    # Scores/mu update daily; only the activated membership revision resets a plan.
    target = {s: [_number(f.get("target_weight_pct")), str(f.get("target_revision") or "")]
              for s, f in sorted(factors.items()) if f.get("selected")}
    week = datetime.fromisoformat(session).isocalendar()[:2]
    return hashlib.sha256(json.dumps([MODEL_KEY, list(week), mode, target], sort_keys=True).encode()).hexdigest()[:24]


def reconcile_plan(plan: dict, trades: list[dict], holdings: list[dict]) -> dict:
    """Consume fills by ledger id, never infer new buys from a cash increase."""
    cursor = int(plan.get("trade_cursor", 0))
    rows = sorted((t for t in trades if int(t.get("id", 0)) > cursor), key=lambda t: int(t.get("id", 0)))
    for trade in rows:
        symbol = str(trade.get("symbol") or "").upper()
        order = plan.get("orders", {}).get(symbol)
        if order:
            remaining = _number(order.get("remaining_units"))
            signed = _number(trade.get("units")) * (1 if str(trade.get("direction")).upper() == "BUY" else -1)
            if remaining*signed > 0:
                order["remaining_units"] = math.copysign(max(0, abs(remaining)-abs(signed)), remaining)
            elif remaining*signed < 0:
                order["remaining_units"] = 0
                order["cancel_reason"] = "manual_opposite_trade" if trade.get("source") == "user_explicit" else "opposite_trade"
        cursor = max(cursor, int(trade.get("id", 0)))
    plan["trade_cursor"] = cursor
    # Inventory changes without ledger fills (e.g. import or corporate action)
    # invalidate that order instead of silently rebuilding a directional trade.
    previous = plan.get("observed_units", {})
    actual = {str(h["symbol"]).upper(): _number(h.get("units")) for h in holdings}
    touched = {str(t.get("symbol") or "").upper() for t in rows}
    for s, order in plan.get("orders", {}).items():
        if s in previous and s not in touched and abs(actual.get(s, 0)-previous[s]) > 0.001:
            order["remaining_units"] = 0
            order["cancel_reason"] = "inventory_changed_outside_plan"
    plan["observed_units"] = actual
    return plan


def prepare_account_plan(
    *, account: str, holdings: list[dict], trades: list[dict], cash: float,
    pending_cash: float, prices: dict, factors: dict, trading_mode: str = "active_profit",
    state_path: Path = STATE_PATH, now: datetime | None = None,
    history_loader: Callable | None = None,
) -> dict:
    """Create/continue one weekly plan under a process-safe state lock."""
    import portalocker

    moment = _local(now)
    completed, session, _ = _clock(moment)
    mode = normalize_trading_mode(trading_mode)
    revision = _revision(factors, session, mode)
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(path)+".lock", timeout=30):
        state = _read(path)
        previous = state["accounts"].get(account) or {}
        if previous.get("revision") == revision:
            plan = reconcile_plan(previous, trades, holdings)
            state["accounts"][account] = plan
            atomic_write_json(path, state)
            return plan
        selected = {s for s, f in factors.items() if f.get("selected")}
        if (not factors or not any(not f.get("low_confidence", True) for f in factors.values())
                or any(factors[s].get("low_confidence", True) for s in selected)):
            raise ValueError("factor_unavailable_for_portfolio")
        if len(selected) > 4:
            raise ValueError("invalid_top4_membership")
        positions = {str(h["symbol"]).upper(): h for h in holdings}
        symbols = sorted(selected | {s for s, h in positions.items() if is_ashare(h) and _number(h.get("units")) > 0})
        used_prices = {}
        for s, h in positions.items():
            if _number(h.get("units")) <= 0:
                continue
            price = _number((prices.get(s) or {}).get("price"))
            if price <= 0 or (prices.get(s) or {}).get("is_stale"):
                raise ValueError("held_security_price_missing")
            used_prices[s] = price
        for s in symbols:
            price = _number((prices.get(s) or {}).get("price"))
            if price <= 0 or (prices.get(s) or {}).get("is_stale"):
                raise ValueError("target_security_price_missing")
            used_prices[s] = price
        cash = max(0, _number(cash))
        total = cash+max(0, _number(pending_cash))+sum(_number(h.get("units"))*used_prices[s] for s, h in positions.items() if _number(h.get("units")) > 0)
        if total <= 0:
            raise ValueError("no_account_equity")
        current = {s: _number(positions.get(s, {}).get("units"))*used_prices[s]/total for s in symbols}
        budget = min(1, sum(current.values())+cash/total)
        reserve = max(0.05, trading_mode_cash_reserve_ratio(mode))
        if history_loader is None:
            from utils.market_data_provider import get_history_data
            # The provider's historical cutoff is exclusive, whereas the risk
            # input must include the last completed session's closing bar.
            cutoff = (datetime.fromisoformat(completed)+timedelta(days=1)).date().isoformat()
            history_loader = lambda s: get_history_data(s, "2y", as_of_date=cutoff, required_through=completed)
        import numpy as np
        covariance = np.zeros((len(symbols), len(symbols)))
        if selected:
            risk_symbols = sorted(selected)
            with ThreadPoolExecutor(max_workers=min(4, len(risk_symbols))) as pool:
                histories = dict(zip(risk_symbols, pool.map(history_loader, risk_symbols)))
            for frame in histories.values():
                if frame is None or frame.empty or str(frame.index[-1])[:10] < completed:
                    raise ValueError("stale_portfolio_history")
            # Outside members have w=0, so their covariance rows cannot affect
            # the objective. Missing old-member history must not block an exit.
            indices = [symbols.index(s) for s in risk_symbols]
            covariance[np.ix_(indices, indices)] = historical_covariance(histories, risk_symbols, as_of=completed)
        result = optimize_transaction_cost_portfolio(symbols=symbols, current_weights=current,
            selected=selected, expected_returns_pct={s: _number(f.get("expected_return_pct")) for s, f in factors.items()},
            covariance=covariance, budget_fraction=budget, fixed_weight=1-budget, cash_reserve=reserve)
        orders = {}
        for s in symbols:
            held = _number(positions.get(s, {}).get("units"))
            weight = result["weights"].get(s, 0)
            target = math.floor(total*weight/used_prices[s]/100)*100
            if s.startswith("688") and held <= 0 and target < 200:
                target = 0
            remaining = target-held
            if remaining < 0 and s in selected:
                remaining = -math.floor(abs(remaining)/100)*100
            orders[s] = {"initial_units": held, "target_units": target, "remaining_units": remaining,
                         "target_weight_pct": weight*100, "selected": s in selected,
                         "reason": "outside_top4_exit" if s not in selected else "cost_regularized_rebalance"}
        plan = {"model": MODEL_KEY, "revision": revision, "account": account,
                "created_at": moment.isoformat(timespec="seconds"), "data_as_of": completed,
                "effective_at": f"{session}T09:30:00+08:00", "week": list(datetime.fromisoformat(session).isocalendar()[:2]),
                "trading_mode": mode, "cash_reserve": reserve, "equity_at_creation": total,
                "trade_cursor": max((int(t.get("id", 0)) for t in trades), default=0),
                "observed_units": {s: _number(h.get("units")) for s, h in positions.items()},
                "orders": orders, "solver": result}
        state["model"] = MODEL_KEY
        state["accounts"][account] = plan
        atomic_write_json(path, state)
        return plan


def plan_decision(
    plan: dict, *, symbol: str, price_info: dict, holdings: list[dict], trades: list[dict],
    cash: float, now: datetime | None = None, require_market_open: bool = True,
) -> dict:
    """Preview the remaining order with fresh inventory/cash/T+1 constraints."""
    plan = reconcile_plan(json.loads(json.dumps(plan)), trades, holdings)
    moment = _local(now)
    _, session, open_now = _clock(moment)
    symbol = str(symbol).upper()
    order = plan.get("orders", {}).get(symbol, {})
    remaining = _number(order.get("remaining_units"))
    held = next((_number(h.get("units")) for h in holdings if str(h.get("symbol")).upper() == symbol), 0)
    price = _number(price_info.get("price"))
    reason = "weekly_plan_complete" if remaining == 0 else str(order.get("reason", "waiting_for_plan"))
    units = abs(remaining)
    if remaining < 0:
        bought_today = sum(_number(t.get("units")) for t in trades if str(t.get("symbol")).upper() == symbol
            and str(t.get("direction")).upper() == "BUY" and str(t.get("trade_date"))[:10] == moment.date().isoformat())
        units = min(units, max(0, held-bought_today))
        if units < 0.001 and remaining:
            reason = "a_share_t1_wait"
        holding = next((h for h in holdings if str(h.get("symbol")).upper() == symbol), {})
        limit = (30 if symbol.startswith(("4", "8", "92")) else
                 20 if symbol.startswith(("300", "301", "688", "689")) else
                 5 if "ST" in str(holding.get("name", "")).upper() else 10)
        if _number(price_info.get("change_pct")) <= -limit+.15:
            units, reason = 0, "limit_down_sell_blocked"
    if price <= 0:
        units, reason = 0, "missing_price"
    if remaining > 0 and price > 0:
        reserve = _number(plan.get("equity_at_creation"))*_number(plan.get("cash_reserve"), .05)
        affordable = max(0, _number(cash)-reserve-PARAMETERS.minimum_fee_cny)/(price*(1+PARAMETERS.fee_rate+PARAMETERS.slippage_rate))
        units = math.floor(min(units, affordable)/100)*100
        if symbol.startswith("688") and held <= 0 and units < 200:
            units = 0
        if units <= 0:
            reason = "waiting_for_cash_or_board_lot"
    if tuple(plan.get("week", [])) != tuple(datetime.fromisoformat(session).isocalendar()[:2]):
        units, reason = 0, "weekly_plan_expired"
    effective = datetime.fromisoformat(plan["effective_at"])
    if moment < effective:
        units, reason = 0, "waiting_for_effective_time"
    if require_market_open and not open_now:
        units, reason = 0, "market_closed"
    if price_info.get("is_stale"):
        units, reason = 0, "stale_quote"
    if price_info.get("fetched_at"):
        try:
            fetched = _local(datetime.fromisoformat(str(price_info["fetched_at"]).replace("Z", "+00:00")))
            age = (moment-fetched).total_seconds()
            if age > int(os.getenv("INVEST_INTRADAY_QUOTE_MAX_AGE_SECONDS", "180")) or age < -60:
                units, reason = 0, "stale_quote"
        except (TypeError, ValueError):
            units, reason = 0, "stale_quote"
    action = ("BUY" if held <= 0 else "ACCUMULATE") if remaining > 0 else ("SELL" if units >= held-0.001 else "TRIM")
    return {"model": MODEL_KEY, "plan_revision": plan.get("revision", ""), "account": plan.get("account"),
            "verdict": action if units > 0 else "HOLD", "units": units,
            "alloc_cny": (1 if remaining > 0 else -1)*units*price if units else 0,
            "target_weight_pct": _number(order.get("target_weight_pct")),
            "remaining_units": remaining, "reason": reason, "effective_at": plan.get("effective_at"),
            "cash_reserve": plan.get("cash_reserve", 0.05), "equity_at_creation": plan.get("equity_at_creation", 0),
            "data_as_of": plan.get("data_as_of"), "selected": bool(order.get("selected")),
            "allow_odd_lot_exit": remaining < 0 and not order.get("selected"),
            "fee_rate": PARAMETERS.fee_rate, "slippage_rate": PARAMETERS.slippage_rate,
            "minimum_fee_cny": PARAMETERS.minimum_fee_cny}


def preview_active_decision(symbol: str, *, account: str, price_info: dict,
                            state_path: Path = STATE_PATH, ledger: Any = None,
                            now: datetime | None = None, require_market_open: bool = True) -> dict:
    owned = ledger is None
    try:
        state = _read(Path(state_path))
        plan = state["accounts"].get(account)
        if not plan or plan.get("model") != MODEL_KEY or plan.get("account") != account:
            return {"model": MODEL_KEY, "verdict": "WAIT", "units": 0, "alloc_cny": 0, "reason": "portfolio_plan_not_ready"}
        if Path(state_path) == STATE_PATH:
            config = json.loads((ROOT/"jobs/market_monitor_config.json").read_text(encoding="utf-8"))
            if normalize_trading_mode(config.get("trading_mode")) != plan.get("trading_mode"):
                return {"model": MODEL_KEY, "verdict": "WAIT", "units": 0, "alloc_cny": 0, "reason": "waiting_for_mode_plan"}
        if owned:
            from db.account_ledger import AccountLedger
            ledger = AccountLedger()
        return plan_decision(plan, symbol=symbol, price_info=price_info,
            holdings=ledger.list_holdings(account), trades=ledger.list_trades(account, limit=100000),
            cash=ledger.account_summary(account).get("cash_cny", 0), now=now,
            require_market_open=require_market_open)
    except Exception as exc:
        log.warning("A-share allocation preview unavailable: %s", type(exc).__name__)
        return {"model": MODEL_KEY, "verdict": "WAIT", "units": 0, "alloc_cny": 0, "reason": "portfolio_plan_unavailable"}
    finally:
        if owned and ledger is not None:
            ledger.close()
