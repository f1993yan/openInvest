"""Execution guards for monitor-generated shadow trades."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from jobs.market_monitor_common import AUTO_TRADE_REPEAT_COOLDOWN_MINUTES, log, _fmt_price, _safe_num
from jobs.market_monitor_entry_exit import evaluate_entry_exit_triggers, evaluate_position_exit_plan_triggers, _stock_units

def _result_direction(result: Dict[str, Any]) -> Optional[str]:
    verdict = str(result.get("verdict", "")).upper()
    alloc = _safe_num(result.get("suggested_alloc_cny"))
    if verdict in {"BUY", "ACCUMULATE"} and alloc > 0:
        return "BUY"
    if verdict in {"TRIM", "SELL"} and alloc < 0:
        return "SELL"
    return None


def _parse_trade_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    except ValueError:
        return None


def _recent_same_direction_committee_trade(
    ledger: Any,
    *,
    symbol: str,
    direction: str,
    cooldown_minutes: int = AUTO_TRADE_REPEAT_COOLDOWN_MINUTES,
) -> Optional[Dict[str, Any]]:
    if ledger is None or cooldown_minutes <= 0:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    try:
        trades = ledger.list_trades("committee", limit=200)
    except Exception as e:  # noqa: BLE001
        log.warning(f"读取影子交易记录失败，跳过重复交易保护: {e}")
        return None
    for trade in trades:
        if str(trade.get("symbol", "")).upper() != symbol.upper():
            continue
        if str(trade.get("direction", "")).upper() != direction:
            continue
        if str(trade.get("source", "")) != "committee_auto":
            continue
        ts = _parse_trade_ts(trade.get("ts"))
        if ts is not None and ts >= cutoff:
            return trade
    return None


def apply_repeated_trade_guard(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    current_price: float,
    ledger: Any,
    entry_exit_state: Dict[str, Any],
) -> Dict[str, Any]:
    direction = _result_direction(result)
    if direction is None:
        return result

    symbol = str(result.get("symbol") or stock.get("symbol") or "").upper()
    # 显式传 cooldown：避免依赖函数默认参数（默认参数在 def 时绑定，曾被模块后段
    # 的重复赋值坑过——实际生效值与 memo 显示值不一致）。现在唯一来源是模块顶部
    # 的 AUTO_TRADE_REPEAT_COOLDOWN_MINUTES（env 可配，默认 240），实际过滤与
    # 下方 memo 写的 cooldown_minutes 必然同值。
    recent_trade = _recent_same_direction_committee_trade(
        ledger,
        symbol=symbol,
        direction=direction,
        cooldown_minutes=AUTO_TRADE_REPEAT_COOLDOWN_MINUTES,
    )
    if not recent_trade:
        return result

    previous = ((entry_exit_state.get("symbols") or {}).get(symbol) or {})
    is_holding = _safe_num(stock.get("position_pct")) > 0 or _stock_units(stock) > 0
    if is_holding and direction == "SELL":
        triggers = evaluate_position_exit_plan_triggers(
            current_price,
            previous.get("position_exit_plan"),
        )
    else:
        triggers = evaluate_entry_exit_triggers(
            current_price,
            previous.get("entry_exit_points"),
            is_holding=False,
        )
    has_direction_trigger = any(t.get("side") == direction.lower() for t in triggers)
    if has_direction_trigger:
        return result

    original_verdict = str(result.get("verdict", ""))
    original_alloc = _safe_num(result.get("suggested_alloc_cny"))
    blocked = dict(result)
    blocked["execution_blocked"] = True
    blocked["execution_block_reason"] = (
        "recent_same_direction_committee_trade_without_new_entry_exit_trigger"
    )
    blocked["optimizer_verdict_before_guard"] = original_verdict
    blocked["optimizer_alloc_cny_before_guard"] = original_alloc
    blocked["verdict"] = "HOLD"
    blocked["suggested_alloc_cny"] = 0
    blocked["confidence"] = min(_safe_num(result.get("confidence"), 0.35), 0.55)
    blocked["cio_memo"] = (
        f"{result.get('cio_memo', '')}\n\n[EXECUTION_GUARD]\n"
        f"side={direction.lower()} blocked=true cooldown_minutes={AUTO_TRADE_REPEAT_COOLDOWN_MINUTES}\n"
        f"last_trade_id={recent_trade.get('id')} last_trade_ts={recent_trade.get('ts')} "
        f"last_price={_fmt_price(recent_trade.get('price'))}\n"
        f"current_price={_fmt_price(current_price)} previous_plan_triggers={triggers or []}\n"
        "reason=同方向影子交易刚执行过，且当前价未触发上一轮买卖点，防止机械重复买/卖。"
    )
    return blocked


def _a_share_limit_up_pct(symbol: str, name: str = "") -> float:
    """A股当日涨停幅度（%）。先按板块定基准，再叠加 ST 折减。

    板块优先于 ST：创业板(300/301)/科创板(688/689) 的 ST 股涨跌停仍是 20%，
    不是主板 ST 的 5%。旧实现把 ST 判断放最前面无视板块，把创业板/科创板 ST
    误判成 5%，过度拦截。北交所(8/4 开头)30%。
    """
    sym = (symbol or "").strip()
    is_st = "ST" in (name or "").upper()
    if sym.startswith(("300", "301", "688", "689")):
        base = 20.0  # 创业板 / 科创板（含 689 CDR）
    elif sym.startswith(("8", "4")):
        base = 30.0  # 北交所
    else:
        base = 10.0  # 沪深主板
    # ST 折减：主板 ST 为 5%；创业板/科创板 ST 仍 20%（不折减），北交所无 ST 概念。
    if is_st and base == 10.0:
        return 5.0
    return base


def _is_limit_up_buy_blocked(result: Dict[str, Any], price_info: Dict[str, Any]) -> bool:
    symbol = str(result.get("symbol", ""))
    market = str(result.get("market", "a")).lower()
    # 只对 A 股做涨停拦截。非 A 股（港股 hk / 美股 us 等无涨跌停或规则不同）一律放行。
    # 旧门控 `market not in {...} and symbol.isdigit() and len(symbol)!=6` 有两个洞：
    # ① 港股代码也是数字且常为 5 位，但当 market 字段缺失退化成 "a" 时不会进此分支；
    # ② 美股等字母代码 symbol.isdigit()=False，条件不成立 → 落到 10% 误判涨停。
    # 改成正向判断：非 A 股直接放行，不再依赖代码形态猜测。
    if market not in {"a", "cn", "ashare"}:
        return False
    change_pct = _safe_num(price_info.get("change_pct"))
    limit_pct = _a_share_limit_up_pct(symbol, str(result.get("name", "")))
    return change_pct >= limit_pct - 0.15


def apply_limit_up_guard(
    result: Dict[str, Any],
    *,
    price_info: Dict[str, Any],
) -> Dict[str, Any]:
    """涨停追高护栏：A股 BUY 类建议遇当日涨停时，改写为 HOLD 并标记 execution_blocked。

    必须在 ledger.apply_committee_result 之前调用。否则只在 select_optimal_actionable_alerts
    里把标的塞进 suppressed（仅抑制用户提醒），影子账户仍会照常在涨停价追买——
    这正是旧实现的漏洞：涨停判断发生在下单之后，护栏对实际下单形同虚设。
    与 apply_repeated_trade_guard 同构（改 verdict=HOLD + alloc=0），让 apply_committee_result
    遇 HOLD 直接 return None，从而真正拦下影子下单。只拦买入方向，不影响卖出/止损。
    """
    direction = _result_direction(result)
    if direction != "BUY":
        return result
    if not _is_limit_up_buy_blocked(result, price_info):
        return result

    original_verdict = str(result.get("verdict", ""))
    original_alloc = _safe_num(result.get("suggested_alloc_cny"))
    blocked = dict(result)
    blocked["execution_blocked"] = True
    blocked["execution_block_reason"] = "limit_up_buy_blocked"
    blocked["optimizer_verdict_before_guard"] = original_verdict
    blocked["optimizer_alloc_cny_before_guard"] = original_alloc
    blocked["verdict"] = "HOLD"
    blocked["suggested_alloc_cny"] = 0
    blocked["cio_memo"] = (
        f"{result.get('cio_memo', '')}\n\n[EXECUTION_GUARD]\n"
        f"limit_up_buy_blocked=true change_pct={_safe_num(price_info.get('change_pct')):.2f}\n"
        "reason=标的当日涨停（或逼近涨停），禁止追高买入，防止影子账户在涨停价成交。"
    )
    return blocked
