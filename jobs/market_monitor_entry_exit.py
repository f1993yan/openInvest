"""Entry/exit trigger state for the market monitor."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from jobs.market_monitor_common import (
    ENTRY_EXIT_ALERT_STATE_PATH,
    log,
    _fmt_price,
    _safe_num,
)

def _stock_units(stock: Dict[str, Any]) -> float:
    return _safe_num(stock.get("units") or stock.get("shares") or stock.get("quantity"))


def _build_initial_position_exit_plan(
    *,
    symbol: str,
    stock: Dict[str, Any],
    result: Dict[str, Any],
    current_price: float,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    _ = (symbol, stock, result, current_price, now)
    return None


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
    _ = (previous_plan, symbol, stock, result, current_price, is_holding, now)
    return None


def evaluate_position_exit_plan_triggers(
    current_price: float,
    position_exit_plan: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    _ = (current_price, position_exit_plan)
    return []


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
    cost_stop_loss_pct: float = 0.0,
) -> List[Dict[str, Any]]:
    """Return buy-side price triggers from the previous technical estimate.

    Sell-side stop/take-profit discipline has been removed. ``is_holding``,
    ``cost``, and ``cost_stop_loss_pct`` remain compatibility arguments only.
    """
    _ = (is_holding, cost, cost_stop_loss_pct)
    if current_price <= 0:
        return []
    entry_exit_points = entry_exit_points or {}

    price = float(current_price)
    buy_pullback = _safe_num(entry_exit_points.get("buy_pullback_price"))
    buy_breakout = _safe_num(entry_exit_points.get("buy_breakout_price"))
    reentry = _safe_num(entry_exit_points.get("reentry_price"))

    triggers: List[Dict[str, Any]] = []

    def _add(side: str, kind: str, level: float) -> None:
        if level > 0:
            triggers.append({
                "side": side,
                "kind": kind,
                "level": round(level, 4),
                "price": round(price, 4),
            })

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
        position_exit_plan = None
        previous_triggers = previous.get("last_triggers") or []
        if is_holding:
            current_triggers = []
        else:
            current_triggers = evaluate_entry_exit_triggers(
                current_price,
                previous.get("entry_exit_points"),
                is_holding=False,
            )
        matched_sides = sorted(_trigger_sides(previous_triggers) & _trigger_sides(current_triggers))
        row = {
            "symbol": symbol,
            "name": result.get("name") or price_info.get("name") or symbol,
            "is_holding": is_holding,
            "current_price": current_price,
            "entry_exit_points": entry_exit_points,
            "position_exit_plan": position_exit_plan,
            "triggers": current_triggers,
            "confirmed": bool(matched_sides),
            "matched_sides": matched_sides,
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
            "position_exit_plan": None,
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
