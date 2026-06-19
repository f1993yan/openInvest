"""Entry/exit trigger state and A-share position exit-plan helpers."""
from __future__ import annotations

import json
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.position_exit_policy import load_position_exit_policy
from jobs.market_monitor_common import (
    COST_STOP_LOSS_PCT,
    ENTRY_EXIT_ALERT_STATE_PATH,
    POSITION_EXIT_PLAN_VERSION,
    log,
    _clamp,
    _fmt_price,
    _round_trade_price,
    _safe_num,
)

def _is_after_a_share_close(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    return now.time() >= dt_time(15, 0)


def _stock_units(stock: Dict[str, Any]) -> float:
    return _safe_num(stock.get("units") or stock.get("shares") or stock.get("quantity"))


def _stock_cost(stock: Dict[str, Any], fallback_price: float = 0.0) -> float:
    cost = _safe_num(stock.get("cost") or stock.get("avg_cost") or stock.get("average_cost"))
    return cost if cost > 0 else _safe_num(fallback_price)


def _effective_atr_pct(entry_exit_points: Optional[Dict[str, Any]], result: Optional[Dict[str, Any]] = None) -> float:
    ee = entry_exit_points or {}
    result = result or {}
    atr_pct = max(
        _safe_num(ee.get("atr_pct")),
        _safe_num(result.get("atr_pct")),
        _safe_num(result.get("volatility_pct")),
        2.0,
    )
    return _clamp(atr_pct, 0.5, 10.0)


def _position_plan_should_reset(
    previous_plan: Optional[Dict[str, Any]],
    *,
    entry_price: float,
    units: float,
    has_explicit_cost: bool = True,
) -> bool:
    if not previous_plan:
        return True
    if int(_safe_num(previous_plan.get("version"))) != POSITION_EXIT_PLAN_VERSION:
        return True
    previous_entry = _safe_num(previous_plan.get("entry_price"))
    previous_units = _safe_num(previous_plan.get("units"))
    if entry_price <= 0 or previous_entry <= 0:
        return True
    if has_explicit_cost and abs(previous_entry - entry_price) / max(entry_price, 0.01) > 0.005:
        return True
    if abs(previous_units - units) >= 1:
        return True
    return False


def _build_initial_position_exit_plan(
    *,
    symbol: str,
    stock: Dict[str, Any],
    result: Dict[str, Any],
    current_price: float,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    entry_exit_points = result.get("entry_exit_points") or {}
    entry_price = _stock_cost(stock, current_price)
    if entry_price <= 0:
        return None
    atr_pct = _effective_atr_pct(entry_exit_points, result)
    policy = load_position_exit_policy(sector=stock.get("sector", ""))
    stop_pct = min(
        policy.max_loss_pct,
        max(3.0, atr_pct * policy.stop_atr_mult),
    )
    hard_stop = entry_price * (1.0 - stop_pct / 100.0)
    risk_per_share = max(entry_price - hard_stop, 0.01)
    take_profit_1 = entry_price + risk_per_share * policy.take_profit_r1
    take_profit_2 = entry_price + risk_per_share * policy.take_profit_r2
    now = now or datetime.now()
    plan = {
        "version": POSITION_EXIT_PLAN_VERSION,
        "plan_type": "a_share_position_exit",
        "symbol": symbol.upper(),
        "entry_price": _round_trade_price(entry_price),
        "units": _stock_units(stock),
        "atr_pct": round(atr_pct, 4),
        "max_loss_pct": round(stop_pct, 4),
        "hard_stop_price": _round_trade_price(hard_stop),
        "effective_stop_price": _round_trade_price(hard_stop),
        "take_profit_1_price": _round_trade_price(take_profit_1),
        "take_profit_2_price": _round_trade_price(take_profit_2),
        "trim_price": _round_trade_price(take_profit_1),
        "highest_close_since_entry": _round_trade_price(current_price if current_price > 0 else entry_price),
        "trailing_stop_price": _round_trade_price(hard_stop),
        "risk_per_share": _round_trade_price(risk_per_share),
        "reward_risk_1": round(policy.take_profit_r1, 4),
        "reward_risk_2": round(policy.take_profit_r2, 4),
        "policy": policy.as_dict(),
        "created_at": now.isoformat(timespec="seconds"),
        "last_updated_after_close": "",
        "locked_intraday": True,
        "reason": "成本锚定的A股持仓纪律：盘中只检查触发，收盘后才允许追踪止损上移。",
    }
    return plan


def _update_position_exit_plan(
    previous_plan: Optional[Dict[str, Any]],
    *,
    symbol: str,
    stock: Dict[str, Any],
    result: Dict[str, Any],
    current_price: float,
    is_holding: bool,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    if not is_holding:
        return None
    has_explicit_cost = _safe_num(stock.get("cost") or stock.get("avg_cost") or stock.get("average_cost")) > 0
    entry_price = _stock_cost(stock, current_price)
    units = _stock_units(stock)
    if _position_plan_should_reset(
        previous_plan,
        entry_price=entry_price,
        units=units,
        has_explicit_cost=has_explicit_cost,
    ):
        return _build_initial_position_exit_plan(
            symbol=symbol,
            stock=stock,
            result=result,
            current_price=current_price,
            now=now,
        )
    plan = dict(previous_plan or {})
    now = now or datetime.now()
    if not _is_after_a_share_close(now):
        return plan

    atr_pct = _safe_num(plan.get("atr_pct"), _effective_atr_pct(result.get("entry_exit_points") or {}, result))
    highest_close = max(_safe_num(plan.get("highest_close_since_entry")), _safe_num(current_price))
    entry_price = _safe_num(plan.get("entry_price"), entry_price)
    policy = load_position_exit_policy(sector=stock.get("sector", ""))
    trail_distance = entry_price * atr_pct * policy.trailing_atr_mult / 100.0
    trailing_stop = highest_close - trail_distance
    effective_stop = max(
        _safe_num(plan.get("effective_stop_price")),
        _safe_num(plan.get("hard_stop_price")),
        trailing_stop,
    )
    plan.update({
        "highest_close_since_entry": _round_trade_price(highest_close),
        "trailing_stop_price": _round_trade_price(trailing_stop),
        "effective_stop_price": _round_trade_price(effective_stop),
        "last_updated_after_close": now.isoformat(timespec="seconds"),
        "locked_intraday": True,
    })
    return plan


def evaluate_position_exit_plan_triggers(
    current_price: float,
    position_exit_plan: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if current_price <= 0 or not position_exit_plan:
        return []
    price = float(current_price)
    stop = _safe_num(position_exit_plan.get("effective_stop_price") or position_exit_plan.get("hard_stop_price"))
    take_1 = _safe_num(position_exit_plan.get("take_profit_1_price"))
    take_2 = _safe_num(position_exit_plan.get("take_profit_2_price"))
    triggers: List[Dict[str, Any]] = []

    def _add(kind: str, level: float) -> None:
        if level > 0:
            triggers.append({
                "side": "sell",
                "kind": kind,
                "level": round(level, 4),
                "price": round(price, 4),
            })

    if stop > 0 and price < stop:
        _add("position_stop", stop)
    if take_2 > 0 and price >= take_2:
        _add("take_profit_2", take_2)
    elif take_1 > 0 and price >= take_1:
        _add("take_profit_1", take_1)
    return triggers


def load_entry_exit_alert_state(state_path: Path = ENTRY_EXIT_ALERT_STATE_PATH) -> Dict[str, Any]:
    if not state_path.exists():
        return {"version": 1, "symbols": {}}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": 1, "symbols": {}}
        data.setdefault("version", 1)
        data.setdefault("symbols", {})
        return data
    except Exception as e:  # noqa: BLE001
        log.warning(f"买卖点触发状态读取失败，重建状态: {e}")
        return {"version": 1, "symbols": {}}


def save_entry_exit_alert_state(
    state: Dict[str, Any],
    state_path: Path = ENTRY_EXIT_ALERT_STATE_PATH,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def evaluate_entry_exit_triggers(
    current_price: float,
    entry_exit_points: Optional[Dict[str, Any]],
    *,
    is_holding: bool,
    cost: float = 0.0,
    cost_stop_loss_pct: float = COST_STOP_LOSS_PCT,
) -> List[Dict[str, Any]]:
    """Return price-level triggers for an existing entry/exit plan.

    The newest plan is generated around the newest price, so this function is
    intended to evaluate the current price against the previously effective
    plan. Consecutive alerting then confirms that two adjacent observations
    crossed compatible levels instead of reacting to a single noisy tick.
    """
    if current_price <= 0:
        return []
    entry_exit_points = entry_exit_points or {}

    price = float(current_price)
    buy_pullback = _safe_num(entry_exit_points.get("buy_pullback_price"))
    buy_breakout = _safe_num(entry_exit_points.get("buy_breakout_price"))
    stop_loss = _safe_num(entry_exit_points.get("stop_loss_price"))
    take_profit = _safe_num(entry_exit_points.get("take_profit_price"))
    trim = _safe_num(entry_exit_points.get("trim_price"))
    reentry = _safe_num(entry_exit_points.get("reentry_price"))
    avg_cost = _safe_num(cost)
    cost_stop_level = 0.0
    if avg_cost > 0 and cost_stop_loss_pct > 0:
        cost_stop_level = avg_cost * (1.0 - cost_stop_loss_pct / 100.0)

    triggers: List[Dict[str, Any]] = []

    def _add(side: str, kind: str, level: float) -> None:
        if level > 0:
            triggers.append({
                "side": side,
                "kind": kind,
                "level": round(level, 4),
                "price": round(price, 4),
            })

    if is_holding:
        if cost_stop_level > 0 and price <= cost_stop_level:
            _add("sell", "cost_stop_loss", cost_stop_level)
        elif stop_loss > 0 and price <= stop_loss:
            _add("sell", "stop_loss", stop_loss)
        elif buy_pullback > 0 and price <= buy_pullback:
            _add("buy", "buy_pullback", buy_pullback)
        elif reentry > 0 and price <= reentry:
            _add("buy", "reentry", reentry)

        if take_profit > 0 and price >= take_profit:
            _add("sell", "take_profit", take_profit)
        elif trim > 0 and price >= trim:
            _add("sell", "trim", trim)
        elif buy_breakout > 0 and price >= buy_breakout:
            _add("buy", "buy_breakout", buy_breakout)
    else:
        if buy_pullback > 0 and price <= buy_pullback:
            _add("buy", "buy_pullback", buy_pullback)
        elif reentry > 0 and price <= reentry:
            _add("buy", "reentry", reentry)
        if buy_breakout > 0 and price >= buy_breakout:
            _add("buy", "buy_breakout", buy_breakout)

    return triggers


def _trigger_sides(triggers: List[Dict[str, Any]]) -> Set[str]:
    return {str(t.get("side", "")) for t in triggers if t.get("side")}


def _trigger_label(kind: Any) -> str:
    return {
        "cost_stop_loss": "成本止损",
        "position_stop": "持仓纪律止损",
        "stop_loss": "技术止损",
        "take_profit": "估算止盈",
        "take_profit_1": "第一止盈",
        "take_profit_2": "第二止盈",
        "trim": "减仓",
        "buy_pullback": "回调买入",
        "buy_breakout": "突破买入",
        "reentry": "重新入场",
    }.get(str(kind or ""), str(kind or "-"))


def update_entry_exit_alert_state(
    *,
    results: List[Dict[str, Any]],
    prices: Dict[str, Dict[str, Any]],
    holding_symbols: Set[str],
    stocks: Optional[List[Dict[str, Any]]] = None,
    state_path: Path = ENTRY_EXIT_ALERT_STATE_PATH,
    now: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    state = load_entry_exit_alert_state(state_path)
    symbols_state = dict(state.get("symbols") or {})
    prices_by_symbol = {str(k).upper(): v for k, v in prices.items()}
    stock_by_symbol = {str(s.get("symbol") or "").upper(): s for s in (stocks or [])}
    holding_symbols = {s.upper() for s in holding_symbols}
    now_dt = now or datetime.now()
    now_text = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    confirmed_alerts: List[Dict[str, Any]] = []
    watch_rows: List[Dict[str, Any]] = []

    for result in results:
        if not result or not result.get("success"):
            continue
        symbol = str(result.get("symbol") or "").upper()
        if not symbol:
            continue
        entry_exit_points = result.get("entry_exit_points") or {}

        price_info = prices_by_symbol.get(symbol) or {}
        current_price = _safe_num(price_info.get("price"), _safe_num(entry_exit_points.get("current_price")))
        is_holding = symbol in holding_symbols
        previous = symbols_state.get(symbol) or {}
        stock = stock_by_symbol.get(symbol) or {}
        position_exit_plan = _update_position_exit_plan(
            previous.get("position_exit_plan"),
            symbol=symbol,
            stock=stock,
            result=result,
            current_price=current_price,
            is_holding=is_holding,
            now=now_dt,
        )
        previous_triggers = previous.get("last_triggers") or []
        if is_holding:
            current_triggers = evaluate_position_exit_plan_triggers(current_price, position_exit_plan)
        else:
            current_triggers = evaluate_entry_exit_triggers(
                current_price,
                previous.get("entry_exit_points"),
                is_holding=False,
            )
        matched_sides = sorted(_trigger_sides(previous_triggers) & _trigger_sides(current_triggers))
        has_immediate_exit_stop = any(
            t.get("kind") in {"cost_stop_loss", "position_stop"} for t in current_triggers
        )

        row = {
            "symbol": symbol,
            "name": result.get("name") or price_info.get("name") or symbol,
            "is_holding": is_holding,
            "current_price": current_price,
            "entry_exit_points": entry_exit_points,
            "position_exit_plan": position_exit_plan,
            "triggers": current_triggers,
            "confirmed": bool(matched_sides) or has_immediate_exit_stop,
            "matched_sides": ["sell"] if has_immediate_exit_stop else matched_sides,
        }
        watch_rows.append(row)

        if row["confirmed"]:
            confirmed_alerts.append({
                **row,
                "previous_triggers": previous_triggers,
            })

        symbols_state[symbol] = {
            "name": row["name"],
            "is_holding": is_holding,
            "current_price": round(current_price, 4),
            "entry_exit_points": entry_exit_points,
            "position_exit_plan": position_exit_plan,
            "last_triggers": current_triggers,
            "last_checked_at": now_text,
        }

    state["version"] = 1
    state["updated_at"] = now_text
    state["symbols"] = symbols_state
    save_entry_exit_alert_state(state, state_path)
    return confirmed_alerts, watch_rows


def format_entry_exit_alert_body(alerts: List[Dict[str, Any]]) -> str:
    lines = ["连续两轮买卖点被价格触发："]
    for alert in alerts[:8]:
        side = "/".join(alert.get("matched_sides") or [])
        trigger_text = ", ".join(
            f"{_trigger_label(t.get('kind'))}@{_fmt_price(t.get('level'))}"
            for t in (alert.get("triggers") or [])[:3]
        )
        lines.append(
            f"{alert['name']}({alert['symbol']}) "
            f"现价{_fmt_price(alert.get('current_price'))} "
            f"{side.upper()} {trigger_text}"
        )
    if len(alerts) > 8:
        lines.append(f"...另有 {len(alerts) - 8} 个标的")
    return "\n".join(lines)
