"""Execution guards for monitor-generated shadow trades."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from jobs.market_monitor_common import AUTO_TRADE_REPEAT_COOLDOWN_MINUTES, log, _fmt_price, _safe_num
from jobs.market_monitor_entry_exit import (
    evaluate_entry_exit_triggers,
    _stock_units,
)

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


def _trade_in_recent_window(
    trade: Dict[str, Any],
    *,
    cooldown_minutes: int,
    trade_date: Optional[str],
) -> bool:
    if trade_date and str(trade.get("trade_date") or "")[:10] == trade_date[:10]:
        return True
    if cooldown_minutes <= 0:
        return False
    ts = _parse_trade_ts(trade.get("ts"))
    if ts is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    return ts >= cutoff


def _recent_account_trade(
    ledger: Any,
    *,
    account: str,
    symbol: str,
    direction: Optional[str] = None,
    cooldown_minutes: int = AUTO_TRADE_REPEAT_COOLDOWN_MINUTES,
    trade_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if ledger is None:
        return None
    try:
        trades = ledger.list_trades(account, limit=200)
    except Exception as e:  # noqa: BLE001
        log.warning(f"读取 {account} 交易记录失败，跳过重复交易保护: {e}")
        return None
    expected_source = "committee_auto" if account == "committee" else "user_explicit"
    for trade in trades:
        if str(trade.get("symbol", "")).upper() != symbol.upper():
            continue
        trade_direction = str(trade.get("direction", "")).upper()
        if direction and trade_direction != direction:
            continue
        if str(trade.get("source", "")) != expected_source:
            continue
        if _trade_in_recent_window(
            trade,
            cooldown_minutes=cooldown_minutes,
            trade_date=trade_date,
        ):
            return trade
    return None


def _recent_same_direction_account_trade(
    ledger: Any,
    *,
    account: str,
    symbol: str,
    direction: str,
    cooldown_minutes: int = AUTO_TRADE_REPEAT_COOLDOWN_MINUTES,
) -> Optional[Dict[str, Any]]:
    if cooldown_minutes <= 0:
        return None
    return _recent_account_trade(
        ledger,
        account=account,
        symbol=symbol,
        direction=direction,
        cooldown_minutes=cooldown_minutes,
        trade_date=None,
    )


def _recent_committee_trade(
    ledger: Any,
    *,
    symbol: str,
    direction: Optional[str] = None,
    cooldown_minutes: int = AUTO_TRADE_REPEAT_COOLDOWN_MINUTES,
    trade_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Backward-compatible committee-account wrapper."""
    return _recent_account_trade(
        ledger,
        account="committee",
        symbol=symbol,
        direction=direction,
        cooldown_minutes=cooldown_minutes,
        trade_date=trade_date,
    )


def _recent_same_direction_committee_trade(
    ledger: Any,
    *,
    symbol: str,
    direction: str,
    cooldown_minutes: int = AUTO_TRADE_REPEAT_COOLDOWN_MINUTES,
) -> Optional[Dict[str, Any]]:
    """Backward-compatible committee-account wrapper."""
    return _recent_same_direction_account_trade(
        ledger,
        account="committee",
        symbol=symbol,
        direction=direction,
        cooldown_minutes=cooldown_minutes,
    )


def _has_direction_trigger(direction: str, triggers: list[Dict[str, Any]]) -> bool:
    side = direction.lower()
    return any(str(t.get("side", "")).lower() == side for t in triggers)


def _trigger_has_price_progress_from_trade(
    *,
    direction: str,
    triggers: list[Dict[str, Any]],
    current_price: float,
    previous_trade: Dict[str, Any],
) -> bool:
    """Whether a fresh trigger moved beyond the last execution price.

    The rule is price-line based instead of time-ban based:
    - pullback/reentry buybacks must be at or below both the trigger line and
      the last sell price;
    - breakout buybacks must be at or above both the breakout line and the last
      sell price;
    - sells are already tied to current stop/take-profit lines.

    That avoids a fixed same-day buyback ban while blocking a same-price flip.
    """
    if not _has_direction_trigger(direction, triggers):
        return False
    last_price = _safe_num(previous_trade.get("price"))
    if last_price <= 0 or current_price <= 0:
        return True
    if direction == "SELL":
        return True
    buy_triggers = [
        t for t in triggers
        if str(t.get("side", "")).lower() == "buy"
    ]
    for trigger in buy_triggers:
        kind = str(trigger.get("kind") or "")
        level = _safe_num(trigger.get("level"))
        if kind in {"buy_pullback", "reentry"}:
            boundary = min(x for x in (level, last_price) if x > 0)
            if current_price <= boundary:
                return True
        elif kind == "buy_breakout":
            boundary = max(level, last_price)
            if current_price >= boundary:
                return True
    return False


def _current_direction_triggers(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    symbol: str,
    direction: str,
    current_price: float,
    entry_exit_state: Dict[str, Any],
) -> list[Dict[str, Any]]:
    previous = ((entry_exit_state.get("symbols") or {}).get(symbol) or {})
    is_holding = _safe_num(stock.get("position_pct")) > 0 or _stock_units(stock) > 0
    if is_holding and direction == "SELL":
        return []
    return evaluate_entry_exit_triggers(
        current_price,
        previous.get("entry_exit_points"),
        is_holding=False,
    )


def _block_execution(
    result: Dict[str, Any],
    *,
    reason: str,
    direction: str,
    current_price: float,
    triggers: list[Dict[str, Any]],
    recent_trade: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    original_verdict = str(result.get("verdict", ""))
    original_alloc = _safe_num(result.get("suggested_alloc_cny"))
    blocked = dict(result)
    blocked["execution_blocked"] = True
    blocked["execution_block_reason"] = reason
    blocked["optimizer_verdict_before_guard"] = original_verdict
    blocked["optimizer_alloc_cny_before_guard"] = original_alloc
    blocked["verdict"] = "HOLD"
    blocked["suggested_alloc_cny"] = 0
    blocked["confidence"] = min(_safe_num(result.get("confidence"), 0.35), 0.55)
    recent_line = ""
    if recent_trade:
        recent_line = (
            f"last_trade_id={recent_trade.get('id')} last_trade_side={recent_trade.get('direction')} "
            f"last_trade_ts={recent_trade.get('ts')} last_price={_fmt_price(recent_trade.get('price'))}\n"
        )
    blocked["cio_memo"] = (
        f"{result.get('cio_memo', '')}\n\n[EXECUTION_GUARD]\n"
        f"side={direction.lower()} blocked=true cooldown_minutes={AUTO_TRADE_REPEAT_COOLDOWN_MINUTES}\n"
        f"{recent_line}"
        f"current_price={_fmt_price(current_price)} previous_plan_triggers={triggers or []}\n"
        f"reason={reason}"
    )
    return blocked


def apply_repeated_trade_guard(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    current_price: float,
    ledger: Any,
    entry_exit_state: Dict[str, Any],
    account: str = "committee",
) -> Dict[str, Any]:
    if account not in {"real", "committee"}:
        raise ValueError("account must be real or committee")
    direction = _result_direction(result)
    if direction is None:
        return result

    symbol = str(result.get("symbol") or stock.get("symbol") or "").upper()
    trade_date = date.today().isoformat()
    triggers = _current_direction_triggers(
        result,
        stock=stock,
        symbol=symbol,
        direction=direction,
        current_price=current_price,
        entry_exit_state=entry_exit_state,
    )
    has_direction_trigger = _has_direction_trigger(direction, triggers)
    # 显式传 cooldown：避免依赖函数默认参数（默认参数在 def 时绑定，曾被模块后段
    # 的重复赋值坑过——实际生效值与 memo 显示值不一致）。现在唯一来源是模块顶部
    # 的 AUTO_TRADE_REPEAT_COOLDOWN_MINUTES（env 可配，默认 240），实际过滤与
    # 下方 memo 写的 cooldown_minutes 必然同值。
    recent_trade = _recent_same_direction_account_trade(
        ledger,
        account=account,
        symbol=symbol,
        direction=direction,
        cooldown_minutes=AUTO_TRADE_REPEAT_COOLDOWN_MINUTES,
    )
    if recent_trade:
        if direction != "SELL" and has_direction_trigger and _trigger_has_price_progress_from_trade(
            direction=direction,
            triggers=triggers,
            current_price=current_price,
            previous_trade=recent_trade,
        ):
            return result
        return _block_execution(
            result,
            reason="recent_same_direction_committee_trade_without_new_entry_exit_trigger",
            direction=direction,
            current_price=current_price,
            triggers=triggers,
            recent_trade=recent_trade,
        )

    recent_symbol_trade = _recent_account_trade(
        ledger,
        account=account,
        symbol=symbol,
        direction=None,
        cooldown_minutes=AUTO_TRADE_REPEAT_COOLDOWN_MINUTES,
        trade_date=trade_date,
    )
    if (
        recent_symbol_trade
        and str(recent_symbol_trade.get("direction", "")).upper() != direction
    ):
        # A committee SELL no longer depends on the retired position-exit
        # price plan. A-share T+1 and sellable-unit checks remain downstream.
        if direction == "SELL":
            return result
        if not has_direction_trigger:
            return _block_execution(
                result,
                reason="recent_opposite_committee_trade_without_new_entry_exit_trigger",
                direction=direction,
                current_price=current_price,
                triggers=triggers,
                recent_trade=recent_symbol_trade,
            )
        if not _trigger_has_price_progress_from_trade(
            direction=direction,
            triggers=triggers,
            current_price=current_price,
            previous_trade=recent_symbol_trade,
        ):
            return _block_execution(
                result,
                reason="recent_opposite_committee_trade_without_price_progress",
                direction=direction,
                current_price=current_price,
                triggers=triggers,
                recent_trade=recent_symbol_trade,
            )
    return result


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
