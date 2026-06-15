"""盘中行情监控 + 委员会分析

每 10 分钟（9:30-15:00）自动拉行情、跑委员会、通知重点操作标的。

启动方式:
  python -m jobs.market_monitor
  或从 start-invest-backend.bat 一并启动
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

# 确保项目根在 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 加载 .env（让本地 LLM 凭据等环境变量生效）
try:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass

REPORT_DIR = _PROJECT_ROOT / "data" / "market_monitor"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("market_monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [market_monitor] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(REPORT_DIR / "monitor.log", encoding="utf-8"),
    ],
)

# ==========================================
# 配置
# ==========================================

CONFIG_PATH = Path(__file__).with_name("market_monitor_config.json")

# 交易时间（北京时间）
TRADING_START = dt_time(9, 30)
LUNCH_START = dt_time(11, 30)
LUNCH_END = dt_time(13, 0)
TRADING_END = dt_time(14, 50)  # 14:50 末轮盯盘，不等到15:00
INTERVAL_MINUTES = max(1, int(os.getenv("INVEST_MONITOR_INTERVAL_MINUTES", "10")))
ENTRY_EXIT_ALERT_STATE_PATH = REPORT_DIR / "entry_exit_alert_state.json"
LATEST_WINDOW_PATH = REPORT_DIR / "latest_window.json"
MONITOR_POPUPS_ENABLED = os.getenv("INVEST_MONITOR_POPUPS", "0") == "1"
AUTO_TRADE_REPEAT_COOLDOWN_MINUTES = max(
    0,
    int(os.getenv("INVEST_AUTO_TRADE_REPEAT_COOLDOWN_MINUTES", "60")),
)

# ==========================================
# 行情拉取
# ==========================================


def fetch_sina_prices(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """从新浪 API 拉取最新行情

    Returns:
        {symbol: {"price": float, "prev_close": float, "change_pct": float, "name": str}}
    """
    sina_map = {
        "600900": "sh600900", "601138": "sh601138", "600487": "sh600487",
        "600183": "sh600183", "601179": "sh601179", "301377": "sz301377",
        "600580": "sh600580", "688099": "sh688099", "000063": "sz000063",
        "002185": "sz002185", "300476": "sz300476", "00700": "hk00700",
        "09988": "hk09988",
    }

    # 自动补充新浪前缀（sz/sh/hk），兜底未知symbol
    def _to_sina(sym):
        if sym in sina_map:
            return sina_map[sym]
        if sym.isdigit() and len(sym) == 6:
            return f"sh{sym}" if sym[0] == "6" else f"sz{sym}"
        if sym.isdigit() and len(sym) == 5:
            return f"hk{sym}"
        return sym

    sina_list = ",".join(_to_sina(s) for s in symbols)
    url = f"https://hq.sinajs.cn/list={sina_list}"

    # 重试3次，应对新浪API偶发抖动
    text = ""
    for attempt in range(3):
        try:
            resp = requests.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=15)
            resp.encoding = "gbk"
            text = resp.text
            if text and 'hq_str_' in text:
                break
            log.warning(f"新浪行情第{attempt+1}次返回空数据，重试...")
            time.sleep(2)
        except Exception as e:
            log.warning(f"新浪行情第{attempt+1}次失败: {e}")
            time.sleep(2)
    if not text or 'hq_str_' not in text:
        log.error(f"新浪行情3次重试均失败")
        return {}

    results = {}
    for line in text.strip().split("\n"):
        if not line.strip() or "=" not in line:
            continue
        try:
            var_name, data = line.split("=", 1)
            # 从 var_name 提取 symbol
            # 格式: var hq_str_sh600900="..."
            symbol_part = var_name.replace("var hq_str_", "").strip()
            # symbol_part like "sh600900" → "600900"
            if symbol_part.startswith("sh"):
                symbol = symbol_part[2:]
            elif symbol_part.startswith("sz"):
                symbol = symbol_part[2:]
            elif symbol_part.startswith("hk"):
                symbol = symbol_part[2:]
            else:
                symbol = symbol_part

            data = data.strip('";\n ')
            fields = data.split(",")

            if len(fields) < 4:
                continue

            name = fields[0]
            # 港股格式不同
            if "hk" in var_name.lower():
                # hk: name, english_name, open, prev_close, price, high, low, ...
                if len(fields) >= 10:
                    prev_close = float(fields[3]) if fields[3] else 0
                    price = float(fields[5]) if fields[5] else 0
                    change_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0
                else:
                    continue
            else:
                # A股: name, open, prev_close, price, high, low, ...
                if len(fields) >= 4:
                    prev_close = float(fields[2]) if fields[2] else 0
                    price = float(fields[3]) if fields[3] else 0
                    change_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0
                else:
                    continue

            results[symbol] = {
                "name": name,
                "price": price,
                "prev_close": prev_close,
                "change_pct": round(change_pct, 2),
            }
        except Exception as e:
            log.warning(f"解析行情行失败: {line[:60]}... {e}")

    return results


# ==========================================
# 委员会 API 调用
# ==========================================


def call_committee(symbol: str, name: str, market: str,
                   position_pct: float, cost: float, current_price: float,
                   total_assets: float, cash: float,
                   all_holdings: List[Dict],
                   target_position_pct: Optional[float] = None,
                   t2_pending: float = 0.0,
                   news_brief: str = "",
                   sector: str = "",
                   industry: str = "",
                   fundamentals: Optional[Dict[str, Any]] = None,
                   optimizer_review_enabled: bool = True) -> Optional[Dict]:
    """调用委员会分析（直接 Python 调用，不需要后端端口）"""
    from backend.server import run_committee_direct, CommitteeRequest, Holding as BackendHolding

    req = CommitteeRequest(
        symbol=symbol,
        name=name,
        market=market,
        sector=sector,
        industry=industry,
        position_pct=position_pct,
        target_position_pct=target_position_pct,
        cost=cost,
        current_price=current_price,
        total_assets=total_assets,
        cash=cash,
        holdings=[BackendHolding(
            symbol=h["symbol"],
            name=h.get("name", ""),
            weight_pct=h.get("weight_pct", h.get("position_pct", 0)),
            cost=h.get("cost", 0),
            current_price=h.get("current_price"),
        ) for h in all_holdings],
        min_lot_size=100 if market == "a" else 100,
        t_plus_1=market == "a",
        available_cash=cash,
        t2_pending_cash=t2_pending,
        news_brief=news_brief,
        fundamentals=fundamentals or {},
        optimizer_review_enabled=optimizer_review_enabled,
        max_debate_rounds=2,  # 监控模式减半辩论轮数，加速
    )
    try:
        response = run_committee_direct(req)
        return response.model_dump()
    except Exception as e:
        log.error(f"委员会 {symbol} 失败: {e}")
        return {"success": False, "symbol": symbol, "error": str(e)}


# ==========================================
# 通知
# ==========================================


def send_windows_toast(title: str, body: str):
    """Windows 通知 — 使用 ctypes MessageBox（非阻塞线程）"""
    import ctypes
    import threading

    def _show():
        try:
            ctypes.windll.user32.MessageBoxW(
                0, body, title,
                0x40 | 0x40000,  # MB_ICONINFORMATION | MB_TOPMOST
            )
        except Exception:
            pass

    threading.Thread(target=_show, daemon=True).start()


def is_scheduled_monitor_popup_time(now: Optional[datetime] = None) -> bool:
    """Only scheduled summary times may popup for news/no-action updates."""
    now = now or datetime.now()
    if now.hour == TRADING_END.hour and now.minute == TRADING_END.minute:
        return True
    return now.minute in (0, 30)


def should_send_monitor_summary_popup(
    *,
    now: Optional[datetime] = None,
    actionable: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Send a summary popup on scheduled times or when trade action is needed."""
    return is_scheduled_monitor_popup_time(now) or bool(actionable)


def _safe_num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_price(value: Any) -> str:
    v = _safe_num(value, 0.0)
    return f"{v:.2f}" if v > 0 else "-"


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
) -> List[Dict[str, Any]]:
    """Return price-level triggers for an existing entry/exit plan.

    The newest plan is generated around the newest price, so this function is
    intended to evaluate the current price against the previously effective
    plan. Consecutive alerting then confirms that two adjacent observations
    crossed compatible levels instead of reacting to a single noisy tick.
    """
    if not entry_exit_points or current_price <= 0:
        return []

    price = float(current_price)
    buy_pullback = _safe_num(entry_exit_points.get("buy_pullback_price"))
    buy_breakout = _safe_num(entry_exit_points.get("buy_breakout_price"))
    stop_loss = _safe_num(entry_exit_points.get("stop_loss_price"))
    take_profit = _safe_num(entry_exit_points.get("take_profit_price"))
    trim = _safe_num(entry_exit_points.get("trim_price"))
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

    if is_holding:
        if stop_loss > 0 and price <= stop_loss:
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


def update_entry_exit_alert_state(
    *,
    results: List[Dict[str, Any]],
    prices: Dict[str, Dict[str, Any]],
    holding_symbols: Set[str],
    state_path: Path = ENTRY_EXIT_ALERT_STATE_PATH,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    state = load_entry_exit_alert_state(state_path)
    symbols_state = dict(state.get("symbols") or {})
    prices_by_symbol = {str(k).upper(): v for k, v in prices.items()}
    holding_symbols = {s.upper() for s in holding_symbols}
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    confirmed_alerts: List[Dict[str, Any]] = []
    watch_rows: List[Dict[str, Any]] = []

    for result in results:
        if not result or not result.get("success"):
            continue
        symbol = str(result.get("symbol") or "").upper()
        if not symbol:
            continue
        entry_exit_points = result.get("entry_exit_points") or {}
        if not entry_exit_points:
            continue

        price_info = prices_by_symbol.get(symbol) or {}
        current_price = _safe_num(price_info.get("price"), _safe_num(entry_exit_points.get("current_price")))
        is_holding = symbol in holding_symbols
        previous = symbols_state.get(symbol) or {}
        previous_triggers = previous.get("last_triggers") or []
        current_triggers = evaluate_entry_exit_triggers(
            current_price,
            previous.get("entry_exit_points"),
            is_holding=is_holding,
        )
        matched_sides = sorted(_trigger_sides(previous_triggers) & _trigger_sides(current_triggers))

        row = {
            "symbol": symbol,
            "name": result.get("name") or price_info.get("name") or symbol,
            "is_holding": is_holding,
            "current_price": current_price,
            "entry_exit_points": entry_exit_points,
            "triggers": current_triggers,
            "confirmed": bool(matched_sides),
            "matched_sides": matched_sides,
        }
        watch_rows.append(row)

        if matched_sides:
            confirmed_alerts.append({
                **row,
                "previous_triggers": previous_triggers,
            })

        symbols_state[symbol] = {
            "name": row["name"],
            "is_holding": is_holding,
            "current_price": round(current_price, 4),
            "entry_exit_points": entry_exit_points,
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
            f"{t.get('kind')}@{_fmt_price(t.get('level'))}"
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
    triggers = evaluate_entry_exit_triggers(
        current_price,
        previous.get("entry_exit_points"),
        is_holding=_safe_num(stock.get("position_pct")) > 0,
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


def _has_llm_hold_conflict(result: Dict[str, Any]) -> bool:
    memo = str(result.get("cio_memo", ""))
    return "LLM=HOLD" in memo and ("-> ACCUMULATE" in memo or "-> BUY" in memo)


def _review_penalty(result: Dict[str, Any]) -> float:
    text = f"{result.get('optimizer_review', '')}\n{result.get('cio_memo', '')}".lower()
    if "reject" in text or "不建议" in text:
        return -45.0
    if "caution" in text or "谨慎" in text or "加仓理由不充分" in text:
        return -30.0
    if "approve" in text or "support" in text:
        return 12.0
    return 0.0


def _entry_trigger_for_result(
    result: Dict[str, Any],
    *,
    current_price: float,
    stock: Dict[str, Any],
    entry_exit_state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    symbol = str(result.get("symbol") or stock.get("symbol") or "").upper()
    previous = ((entry_exit_state.get("symbols") or {}).get(symbol) or {})
    return evaluate_entry_exit_triggers(
        current_price,
        previous.get("entry_exit_points"),
        is_holding=_safe_num(stock.get("position_pct")) > 0,
    )


def _action_score(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    price_info: Dict[str, Any],
    triggers: List[Dict[str, Any]],
) -> float:
    confidence = _safe_num(result.get("confidence"), 0.0)
    score = 100.0 * confidence
    verdict = str(result.get("verdict", "")).upper()
    if verdict == "BUY":
        score += 8.0
    elif verdict == "ACCUMULATE":
        score += 2.0
    elif verdict in {"TRIM", "SELL"}:
        score += 5.0
    score += _review_penalty(result)
    if _has_llm_hold_conflict(result):
        score -= 35.0
    if any(t.get("side") == "buy" for t in triggers):
        score += 24.0
    elif verdict in {"BUY", "ACCUMULATE"}:
        score -= 18.0
    if any(t.get("side") == "sell" for t in triggers):
        score += 20.0

    fundamental_score = result.get("fundamental_score")
    if fundamental_score is not None:
        score += (_safe_num(fundamental_score, 50.0) - 50.0) * 0.35

    position_pct = _safe_num(stock.get("position_pct"))
    if position_pct > 20:
        score -= 12.0
    elif position_pct < 5 and verdict in {"BUY", "ACCUMULATE"}:
        score += 4.0

    change_pct = _safe_num(price_info.get("change_pct"))
    if verdict in {"BUY", "ACCUMULATE"} and change_pct > 7.0:
        score -= 10.0

    ee = result.get("entry_exit_points") or {}
    rr = _safe_num(ee.get("reward_risk_ratio"))
    if rr > 0:
        score += min(12.0, rr * 4.0)
    return score


def _stock_sector(stock: Dict[str, Any]) -> str:
    return str(stock.get("sector") or stock.get("industry") or "UNKNOWN")


def _existing_sector_exposure(stocks: List[Dict[str, Any]]) -> Dict[str, float]:
    exposure: Dict[str, float] = {}
    for stock in stocks:
        sector = _stock_sector(stock)
        exposure[sector] = exposure.get(sector, 0.0) + _safe_num(stock.get("position_pct"))
    return exposure


def _option_risk_penalty(
    *,
    result: Dict[str, Any],
    stock: Dict[str, Any],
    price: float,
    cost: float,
    portfolio_value: float,
    max_single_position_pct: float,
) -> Tuple[float, Dict[str, float]]:
    """Return a deterministic risk penalty and metrics for one buy option."""
    ee = result.get("entry_exit_points") or {}
    add_pct = cost / max(portfolio_value, 1.0) * 100.0
    current_pct = _safe_num(stock.get("position_pct"))
    post_position_pct = current_pct + add_pct
    target_pct = _safe_num(stock.get("target_position_pct") or stock.get("target_pct"), max_single_position_pct)

    stop_loss = _safe_num(ee.get("stop_loss_price"))
    stop_loss_pct = 0.0
    if price > 0 and stop_loss > 0 and stop_loss < price:
        stop_loss_pct = (price - stop_loss) / price * 100.0
    else:
        stop_loss_pct = max(_safe_num(ee.get("atr_pct")), 2.0)

    rr = _safe_num(ee.get("reward_risk_ratio"))
    penalty = 0.0
    penalty += max(0.0, stop_loss_pct - 5.0) * 1.8
    penalty += max(0.0, post_position_pct - target_pct) * 1.2
    penalty += max(0.0, post_position_pct - max_single_position_pct) * 5.0
    if rr > 0 and rr < 1.5:
        penalty += (1.5 - rr) * 12.0

    return penalty, {
        "add_position_pct": round(add_pct, 4),
        "post_position_pct": round(post_position_pct, 4),
        "stop_loss_pct": round(stop_loss_pct, 4),
        "reward_risk_ratio": round(rr, 4),
    }


def _distribution_penalty(
    option: Dict[str, Any],
    chosen: List[Dict[str, Any]],
    *,
    existing_sector_pct: Dict[str, float],
    max_sector_position_pct: float,
) -> float:
    """Penalize sector crowding across the selected alert basket."""
    sector = str(option.get("alert_sector") or "UNKNOWN")
    selected_sector_pct = sum(
        _safe_num(item.get("alert_add_position_pct"))
        for item in chosen
        if str(item.get("alert_sector") or "UNKNOWN") == sector
    )
    after_sector_pct = existing_sector_pct.get(sector, 0.0) + selected_sector_pct + _safe_num(option.get("alert_add_position_pct"))
    sector_penalty = max(0.0, after_sector_pct - max_sector_position_pct) * 4.0

    symbol = str(option.get("symbol", "")).upper()
    repeated_symbol_penalty = 0.0
    if any(str(item.get("symbol", "")).upper() == symbol for item in chosen):
        repeated_symbol_penalty = 999.0

    diversification_bonus = 0.0
    if sector != "UNKNOWN" and not any(str(item.get("alert_sector") or "UNKNOWN") == sector for item in chosen):
        diversification_bonus = 4.0
    return sector_penalty + repeated_symbol_penalty - diversification_bonus


def select_optimal_actionable_alerts(
    *,
    results: List[Dict[str, Any]],
    prices: Dict[str, Dict[str, Any]],
    cash: float,
    stocks: List[Dict[str, Any]],
    entry_exit_state: Dict[str, Any],
    max_alerts: int = 4,
    portfolio_value: Optional[float] = None,
    max_single_position_pct: float = 25.0,
    max_sector_position_pct: float = 35.0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Pick executable alerts under cash and risk-distribution budgets.

    This is a multiple-choice knapsack over board-lot buy options, with sells
    admitted separately because they release risk/cash instead of consuming it.
    Buy candidates are scored by signal quality minus stop-loss, position, and
    sector-concentration penalties, so the selected basket is diversified rather
    than merely the highest raw score that fits the cash budget.
    """
    stock_by_symbol = {str(s.get("symbol", "")).upper(): s for s in stocks}
    existing_sector_pct = _existing_sector_exposure(stocks)
    # If the caller does not provide total assets, treat available cash as
    # roughly a 20% cash sleeve. This keeps legacy tests/callers from being
    # over-penalized as if every buy consumed nearly the whole portfolio.
    inferred_portfolio_value = max(_safe_num(cash) * 5.0, _safe_num(cash), 1.0)
    base_portfolio_value = max(_safe_num(portfolio_value), inferred_portfolio_value)
    suppressed: List[Dict[str, Any]] = []
    sell_alerts: List[Dict[str, Any]] = []
    buy_groups: List[List[Dict[str, Any]]] = []

    for result in results:
        if not result or not result.get("success") or result.get("execution_blocked"):
            if result and result.get("execution_blocked"):
                suppressed.append({
                    "symbol": result.get("symbol"),
                    "name": result.get("name"),
                    "reason": "execution_guard",
                })
            continue
        verdict = str(result.get("verdict", "")).upper()
        alloc = _safe_num(result.get("suggested_alloc_cny"))
        if verdict in {"HOLD", "REDUCE", "UNCLEAR"} or abs(alloc) <= 0:
            continue

        symbol = str(result.get("symbol", "")).upper()
        stock = stock_by_symbol.get(symbol, {})
        price_info = prices.get(symbol) or prices.get(str(result.get("symbol", ""))) or {}
        price = _safe_num(price_info.get("price"))
        if price <= 0:
            suppressed.append({"symbol": symbol, "name": result.get("name"), "reason": "missing_price"})
            continue
        triggers = _entry_trigger_for_result(
            result,
            current_price=price,
            stock=stock,
            entry_exit_state=entry_exit_state,
        )
        score = _action_score(result, stock=stock, price_info=price_info, triggers=triggers)

        if verdict in {"TRIM", "SELL"} and alloc < 0:
            if score >= 45.0:
                selected = dict(result)
                selected["alert_score"] = round(score, 2)
                selected["alert_triggers"] = triggers
                sell_alerts.append(selected)
            else:
                suppressed.append({"symbol": symbol, "name": result.get("name"), "reason": "low_sell_score"})
            continue

        if verdict not in {"BUY", "ACCUMULATE"} or alloc <= 0:
            continue
        if _is_limit_up_buy_blocked(result, price_info):
            suppressed.append({"symbol": symbol, "name": result.get("name"), "reason": "limit_up_buy_blocked"})
            continue
        if _has_llm_hold_conflict(result) and not any(t.get("side") == "buy" for t in triggers):
            suppressed.append({"symbol": symbol, "name": result.get("name"), "reason": "llm_hold_without_buy_trigger"})
            continue
        if score < 55.0:
            suppressed.append({"symbol": symbol, "name": result.get("name"), "reason": f"low_score:{score:.1f}"})
            continue

        lot = int(stock.get("min_lot_size") or result.get("min_lot_size") or 100)
        lot = max(lot, 1)
        lot_cost = price * lot
        suggested_lots = int(abs(alloc) // lot_cost)
        affordable_lots = int(max(cash, 0.0) // lot_cost)
        max_lots = min(max(suggested_lots, 1), affordable_lots)
        if max_lots <= 0:
            suppressed.append({"symbol": symbol, "name": result.get("name"), "reason": "cash_insufficient"})
            continue

        group: List[Dict[str, Any]] = []
        for lots in range(1, max_lots + 1):
            cost = lots * lot_cost
            risk_penalty, risk_metrics = _option_risk_penalty(
                result=result,
                stock=stock,
                price=price,
                cost=cost,
                portfolio_value=base_portfolio_value,
                max_single_position_pct=max_single_position_pct,
            )
            base_value = score * (lots ** 0.5)
            option = dict(result)
            option["suggested_alloc_cny"] = round(cost)
            option["alert_selected_lots"] = lots
            option["alert_score"] = round(score, 2)
            option["alert_raw_value"] = round(base_value, 4)
            option["alert_risk_penalty"] = round(risk_penalty, 4)
            option["alert_value"] = round(base_value - risk_penalty, 4)
            option["alert_cost_cny"] = cost
            option["alert_triggers"] = triggers
            option["alert_sector"] = _stock_sector(stock)
            option["alert_add_position_pct"] = risk_metrics["add_position_pct"]
            option["alert_post_position_pct"] = risk_metrics["post_position_pct"]
            option["alert_stop_loss_pct"] = risk_metrics["stop_loss_pct"]
            group.append(option)
        buy_groups.append(group)

    buy_limit = max(0, max_alerts - len(sell_alerts))
    budget_unit = 10.0
    budget = int(max(cash, 0.0) // budget_unit)
    dp: Dict[int, Tuple[float, List[Dict[str, Any]]]] = {0: (0.0, [])}
    for group in buy_groups:
        next_dp = dict(dp)
        for used, (value, chosen) in dp.items():
            if len(chosen) >= buy_limit:
                continue
            for option in group:
                cost_units = int(_safe_num(option.get("alert_cost_cny")) // budget_unit)
                new_used = used + cost_units
                if new_used > budget:
                    continue
                dist_penalty = _distribution_penalty(
                    option,
                    chosen,
                    existing_sector_pct=existing_sector_pct,
                    max_sector_position_pct=max_sector_position_pct,
                )
                new_value = value + _safe_num(option.get("alert_value")) - dist_penalty
                if new_value > next_dp.get(new_used, (-1.0, []))[0]:
                    selected_option = dict(option)
                    selected_option["alert_distribution_penalty"] = round(dist_penalty, 4)
                    selected_option["alert_portfolio_value"] = round(new_value, 4)
                    next_dp[new_used] = (new_value, chosen + [selected_option])
        dp = next_dp
    selected_buys = max(dp.values(), key=lambda item: item[0])[1] if dp else []
    selected_buy_symbols = {str(r.get("symbol", "")).upper() for r in selected_buys}
    for group in buy_groups:
        symbol = str(group[0].get("symbol", "")).upper() if group else ""
        if symbol and symbol not in selected_buy_symbols:
            suppressed.append({
                "symbol": symbol,
                "name": group[0].get("name") if group else symbol,
                "reason": "cash_budget_not_selected_by_utility",
            })

    selected = sell_alerts + selected_buys
    selected.sort(key=lambda r: _safe_num(r.get("alert_score")), reverse=True)
    return selected[:max_alerts], suppressed


def _first_prefixed_line(text: str, prefixes: Tuple[str, ...]) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        for prefix in prefixes:
            if upper.startswith(prefix):
                return stripped.split(":", 1)[1].strip() if ":" in stripped else stripped
    return ""


def _operation_detail(result: Dict[str, Any], row: Dict[str, Any], actionable: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    symbol = str(result.get("symbol") or row.get("symbol") or "").upper()
    action_row = actionable.get(symbol)
    verdict = str(result.get("verdict") or "UNKNOWN").upper()
    alloc = _safe_num(result.get("suggested_alloc_cny"))
    triggers = row.get("triggers") or []
    if action_row:
        status = "action_required"
        reason = "selected_by_cash_risk_optimizer"
    elif row.get("confirmed"):
        status = "trigger_confirmed"
        reason = "entry_exit_price_confirmed_two_rounds"
    elif triggers:
        status = "watch_trigger"
        reason = "price_touched_one_round_trigger"
    elif verdict in {"BUY", "ACCUMULATE", "TRIM", "SELL"} and abs(alloc) > 0:
        status = "candidate"
        reason = "committee_has_direction_but_not_selected"
    elif result.get("execution_blocked"):
        status = "blocked"
        reason = str(result.get("execution_block_reason") or "execution_guard")
    elif result.get("success"):
        status = "monitoring"
        reason = "no_action_now"
    else:
        status = "error"
        reason = str(result.get("error") or "analysis_failed")
    return {
        "status": status,
        "reason": reason,
        "verdict": verdict,
        "confidence": round(_safe_num(result.get("confidence")), 4),
        "suggested_alloc_cny": round(alloc, 2),
        "alert_score": None if not action_row else round(_safe_num(action_row.get("alert_score")), 2),
        "triggers": triggers,
        "confirmed": bool(row.get("confirmed")),
        "llm_conflict": _has_llm_hold_conflict(result),
        "execution_blocked": bool(result.get("execution_blocked")),
    }


def _llm_operation_review(result: Dict[str, Any]) -> Dict[str, str]:
    review = result.get("optimizer_review") or ""
    memo = result.get("cio_memo") or ""
    return {
        "conclusion": _first_prefixed_line(review, ("CONCLUSION:",)),
        "one_line": _first_prefixed_line(review, ("ONE_LINE:",)),
        "risk_note": _first_prefixed_line(memo, ("RISK_PLAN:", "RISK:",)),
        "execution_plan": _first_prefixed_line(memo, ("EXECUTION_PLAN:",)),
        "raw_excerpt": (review or memo)[:1200],
    }


def build_monitor_window_snapshot(
    *,
    round_time: str,
    results: List[Dict[str, Any]],
    actionable: List[Dict[str, Any]],
    prices: Dict[str, Dict[str, Any]],
    stocks: List[Dict[str, Any]],
    entry_exit_watch: List[Dict[str, Any]],
    entry_exit_alerts: List[Dict[str, Any]],
    suppressed_alerts: List[Dict[str, Any]],
    cash: float,
    total_assets: float,
) -> Dict[str, Any]:
    """Build the stable monitor-window payload consumed by the desktop UI."""
    result_by_symbol = {str(r.get("symbol") or "").upper(): r for r in results if r}
    stock_by_symbol = {str(s.get("symbol") or "").upper(): s for s in stocks if s.get("symbol")}
    price_by_symbol = {str(k).upper(): v for k, v in prices.items()}
    watch_by_symbol = {str(row.get("symbol") or "").upper(): row for row in entry_exit_watch}
    actionable_by_symbol = {str(row.get("symbol") or "").upper(): row for row in actionable}
    suppressed_by_symbol: Dict[str, List[str]] = {}
    for item in suppressed_alerts:
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            suppressed_by_symbol.setdefault(symbol, []).append(str(item.get("reason") or "suppressed"))

    symbols = sorted(set(stock_by_symbol) | set(result_by_symbol) | set(price_by_symbol) | set(watch_by_symbol))
    rows: List[Dict[str, Any]] = []
    for symbol in symbols:
        stock = stock_by_symbol.get(symbol, {})
        result = result_by_symbol.get(symbol, {"success": False, "symbol": symbol, "name": stock.get("name", symbol)})
        price_info = price_by_symbol.get(symbol, {})
        row = watch_by_symbol.get(symbol, {})
        ee = result.get("entry_exit_points") or row.get("entry_exit_points") or {}
        operation = _operation_detail(result, row, actionable_by_symbol)
        rows.append({
            "symbol": symbol,
            "name": result.get("name") or stock.get("name") or price_info.get("name") or symbol,
            "market": result.get("market") or stock.get("market", "a"),
            "sector": stock.get("sector", ""),
            "industry": stock.get("industry", ""),
            "min_lot_size": int(_safe_num(stock.get("min_lot_size"), 100) or 100),
            "units": round(_safe_num(stock.get("units")), 4),
            "is_holding": _safe_num(stock.get("position_pct")) > 0 or _safe_num(stock.get("units")) > 0,
            "position_pct": round(_safe_num(stock.get("position_pct")), 4),
            "target_position_pct": stock.get("target_position_pct", stock.get("target_pct")),
            "cost": _safe_num(stock.get("cost")),
            "price": {
                "current": _safe_num(price_info.get("price"), _safe_num(ee.get("current_price"))),
                "prev_close": _safe_num(price_info.get("prev_close")),
                "change_pct": _safe_num(price_info.get("change_pct")),
                "name": price_info.get("name", ""),
            },
            "state": operation["status"],
            "buy_criteria": {
                "pullback_price": _safe_num(ee.get("buy_pullback_price")),
                "breakout_price": _safe_num(ee.get("buy_breakout_price")),
                "reentry_price": _safe_num(ee.get("reentry_price")),
                "reward_risk_ratio": _safe_num(ee.get("reward_risk_ratio")),
                "reason": ee.get("reason", ""),
            },
            "exit_points": {
                "stop_loss_price": _safe_num(ee.get("stop_loss_price")),
                "take_profit_price": _safe_num(ee.get("take_profit_price")),
                "trim_price": _safe_num(ee.get("trim_price")),
            },
            "fundamental": {
                "model": result.get("fundamental_model", ""),
                "score": _safe_num(result.get("fundamental_score"), 50.0),
                "coverage": _safe_num(result.get("fundamental_coverage")),
                "anchor_multiplier": _safe_num(result.get("fundamental_anchor_multiplier"), 1.0),
            },
            "technical": {
                "regime": result.get("regime", ""),
                "quant_view": result.get("quant_view", ""),
                "market_data_excerpt": result.get("market_data", "")[:1200],
                "entry_exit_model": ee.get("model_name", ""),
                "low_confidence": bool(ee.get("low_confidence")),
                "atr_pct": _safe_num(ee.get("atr_pct")),
                "expected_return_pct": _safe_num(ee.get("expected_return_pct")),
            },
            "operation": operation,
            "llm_review": _llm_operation_review(result),
            "suppressed_reasons": suppressed_by_symbol.get(symbol, []),
            "error": result.get("error", ""),
            "success": bool(result.get("success")),
        })

    return {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "round_time": round_time,
        "cash_cny": round(_safe_num(cash), 2),
        "total_assets_cny": round(_safe_num(total_assets), 2),
        "counts": {
            "symbols": len(rows),
            "action_required": sum(1 for row in rows if row["state"] == "action_required"),
            "entry_exit_alerts": len(entry_exit_alerts),
            "suppressed": len(suppressed_alerts),
            "errors": sum(1 for row in rows if not row["success"]),
        },
        "rows": rows,
        "actionable": actionable,
        "entry_exit_alerts": entry_exit_alerts,
        "suppressed_alerts": suppressed_alerts,
    }


def write_monitor_window_snapshot(snapshot: Dict[str, Any], path: Path = LATEST_WINDOW_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_report(round_time: str, results: List[Dict],
                 actionable: List[Dict], prices: Dict,
                 news_items: List = None,
                 entry_exit_watch: Optional[List[Dict[str, Any]]] = None,
                 entry_exit_alerts: Optional[List[Dict[str, Any]]] = None,
                 suppressed_alerts: Optional[List[Dict[str, Any]]] = None):
    """写入监控报告"""
    report_path = REPORT_DIR / f"monitor_{round_time.replace(':', '-')}.md"

    lines = [
        f"# 📊 盘中监控报告 — {round_time}",
        "",
        "## 📈 最新行情",
        "",
        "| 股票 | 代码 | 最新价 | 涨跌幅 |",
        "|------|------|--------|--------|",
    ]
    for sym, p in sorted(prices.items()):
        lines.append(f"| {p['name']} | {sym} | ¥{p['price']:.2f} | {p['change_pct']:+.2f}% |")

    # 新闻摘要
    if news_items:
        from collections import Counter
        src_counts = Counter(i.src_name for i in news_items)
        lines.extend([
            "",
            "## 📰 国内新闻摘要",
            "",
            f"共 {len(news_items)} 条（{', '.join(f'{k}:{v}' for k,v in sorted(src_counts.items()))}）",
            "",
        ])
        sector_summary = []
        try:
            from services.news_sources.domestic_hot_news import summarize_sector_hits
            sector_summary = summarize_sector_hits(news_items)
        except Exception as e:
            log.warning(f"新闻板块汇总失败: {e}")

        if sector_summary:
            lines.extend([
                "### 涉及板块与龙头",
                "",
                "| 板块 | 热度 | 龙头股票 | 新闻源 |",
                "|------|------|----------|--------|",
            ])
            for row in sector_summary:
                leaders = "、".join(
                    f"{leader.get('name')}({leader.get('symbol')})"
                    for leader in row.get("leaders", [])
                    if leader.get("name") and leader.get("symbol")
                )
                lines.append(
                    f"| {row.get('sector')} | {row.get('count')} | {leaders or '-'} | "
                    f"{', '.join(row.get('sources', []))} |"
                )
            lines.append("")
            lines.append("### 新闻条目")
            lines.append("")

        try:
            from services.news_sources.news_risk_model import summarize_risk_impacts
            risk_rows = summarize_risk_impacts(news_items)
        except Exception as e:
            log.warning(f"新闻风险量化失败: {e}")
            risk_rows = []

        if risk_rows:
            lines.extend([
                "### 重大危险信息量化影响",
                "",
                "| 新闻源 | 风险类别 | CSI300冲击 | 90%区间 | 最大板块冲击 | 模型证据 |",
                "|--------|----------|------------|---------|--------------|----------|",
            ])
            for row in risk_rows:
                ci = row.get("ci90_bps", ["?", "?"])
                top_sector = (row.get("sector_impacts") or [{}])[0]
                sector_text = (
                    f"{top_sector.get('sector')} {top_sector.get('impact_bps'):+.1f}bp"
                    if top_sector.get("sector") else "-"
                )
                lines.append(
                    f"| {row.get('src_name')} | {row.get('category')} | "
                    f"{row.get('csi300_impact_bps'):+.1f}bp | "
                    f"{ci[0]:+.1f}~{ci[1]:+.1f}bp | {sector_text} | "
                    f"{', '.join(row.get('evidence_keywords', [])[:4])} |"
                )
            lines.extend([
                "",
                "> 模型: event_study_prior_capm_v1，公式 = -base_bps(category) * severity * source_reliability * a_share_proximity。",
                "",
            ])

        for item in news_items[:10]:
            title = item.title[:80] if item.title else ""
            if not title:
                continue
            lines.append(f"- [{item.src_name}] {title}")
            for sector in (item.raw_meta or {}).get("sectors", [])[:2]:
                leaders = "、".join(
                    f"{leader.get('name')}({leader.get('symbol')})"
                    for leader in sector.get("leaders", [])[:3]
                    if leader.get("name") and leader.get("symbol")
                )
                if leaders:
                    lines.append(f"  - 涉及板块: {sector.get('sector')}；对应龙头: {leaders}")
            for impact in (item.raw_meta or {}).get("risk_impacts", [])[:1]:
                ci = impact.get("ci90_bps", ["?", "?"])
                lines.append(
                    f"  - 危险信号: {impact.get('category')}；"
                    f"CSI300模型冲击 {impact.get('csi300_impact_bps'):+.1f}bp "
                    f"(90%区间 {ci[0]:+.1f}~{ci[1]:+.1f}bp)"
                )

    lines.extend([
        "",
        "## 🏛️ 委员会分析汇总",
        "",
        "| 股票 | Verdict | 置信度 | 建议 | Fundamental | OptReview | Quant | Regime |",
        "|------|---------|--------|------|-------------|-----------|-------|--------|",
    ])
    for r in results:
        if r.get("success"):
            fundamental = r.get("fundamental_model", "")
            if r.get("fundamental_score") is not None:
                fundamental = f"{fundamental}:{r.get('fundamental_score', 50):.0f}"
            review = ""
            for line in (r.get("optimizer_review") or "").splitlines():
                if line.startswith("CONCLUSION:"):
                    review = line.split(":", 1)[1].strip()
                    break
            lines.append(
                f"| {r['name']} | **{r['verdict']}** | {r['confidence']:.2f} | "
                f"¥{r.get('suggested_alloc_cny', 0):,.0f} | "
                f"{fundamental} | "
                f"{review or '-'} | "
                f"{r.get('quant_view', '')[:80]}... | "
                f"{r.get('regime', '')} |"
            )
        else:
            lines.append(f"| {r.get('name', r.get('symbol'))} | ❌ 失败 | — | — | — | — | — | — |")

    guarded = [r for r in results if r.get("execution_blocked")]
    if guarded:
        lines.extend([
            "",
            "## 🧯 执行保护",
            "",
        ])
        for r in guarded:
            lines.append(
                f"- {r.get('name')} ({r.get('symbol')}): "
                f"{r.get('optimizer_verdict_before_guard')} "
                f"¥{_safe_num(r.get('optimizer_alloc_cny_before_guard')):,.0f} -> HOLD；"
                f"{r.get('execution_block_reason')}"
            )

    if suppressed_alerts:
        lines.extend([
            "",
            "## 🧮 提醒优化过滤",
            "",
        ])
        for item in suppressed_alerts[:20]:
            lines.append(
                f"- {item.get('name') or item.get('symbol')}: {item.get('reason')}"
            )

    if entry_exit_watch:
        lines.extend([
            "",
            "## 🎯 买入卖出点监控",
            "",
            "| 股票 | 类型 | 现价 | 买回调 | 买突破 | 止损 | 止盈 | 减仓 | 触发状态 |",
            "|------|------|------|--------|--------|------|------|------|----------|",
        ])
        for row in entry_exit_watch:
            ee = row.get("entry_exit_points") or {}
            trigger_text = "、".join(
                f"{t.get('side')}:{t.get('kind')}@{_fmt_price(t.get('level'))}"
                for t in (row.get("triggers") or [])[:3]
            )
            if row.get("confirmed"):
                trigger_text = f"连续触发({trigger_text})"
            lines.append(
                f"| {row.get('name')} ({row.get('symbol')}) | "
                f"{'持仓' if row.get('is_holding') else '关注'} | "
                f"{_fmt_price(row.get('current_price'))} | "
                f"{_fmt_price(ee.get('buy_pullback_price'))} | "
                f"{_fmt_price(ee.get('buy_breakout_price'))} | "
                f"{_fmt_price(ee.get('stop_loss_price'))} | "
                f"{_fmt_price(ee.get('take_profit_price'))} | "
                f"{_fmt_price(ee.get('trim_price'))} | "
                f"{trigger_text or '-'} |"
            )

    if entry_exit_alerts:
        lines.extend([
            "",
            "## 🚨 连续触发提醒",
            "",
        ])
        for alert in entry_exit_alerts:
            lines.append(
                f"- {alert.get('name')} ({alert.get('symbol')}) "
                f"现价 {_fmt_price(alert.get('current_price'))} "
                f"方向 {','.join(alert.get('matched_sides') or [])}"
            )

    if actionable:
        lines.extend([
            "",
            "## 🎯 ⚠️ 需要操作的标的",
            "",
        ])
        for i, a in enumerate(actionable, 1):
            lines.append(
                f"### {i}. {a['name']} ({a['symbol']}) — **{a['verdict']}** "
                f"(置信度: {a['confidence']:.2f})"
            )
            if a.get("cio_memo"):
                # 提取 PERSONAL_NOTE 段落
                memo = a["cio_memo"]
                if "PERSONAL_NOTE:" in memo:
                    note = memo.split("PERSONAL_NOTE:")[1].split("\n\n")[0]
                    lines.append(f"")
                    lines.append(f"> {note.strip()}")
                elif "EXECUTION_PLAN:" in memo:
                    plan = memo.split("EXECUTION_PLAN:")[1].split("RISK_PLAN:")[0]
                    lines.append(f"```")
                    lines.append(f"EXECUTION_PLAN:{plan.strip()}")
                    lines.append(f"```")
            lines.append("")

    lines.extend([
        "---",
        f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
    ])

    report_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"报告已写入: {report_path}")

    # 同时写入最新摘要
    latest_path = REPORT_DIR / "latest.md"
    latest_path.write_text("\n".join(lines), encoding="utf-8")


# ==========================================
# 主循环
# ==========================================


def is_trading_time() -> bool:
    """判断当前是否在交易时段内（周一至周五 9:30-11:30, 13:00-15:00）"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周六日
        return False
    t = now.time()
    return (TRADING_START <= t <= LUNCH_START) or (LUNCH_END <= t <= TRADING_END)


def wait_until_next_round():
    """等待到下一个监控间隔边界"""
    now = datetime.now()
    interval = max(1, INTERVAL_MINUTES)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_minutes = now.hour * 60 + now.minute
    next_slot_minutes = ((elapsed_minutes // interval) + 1) * interval
    target = day_start + timedelta(minutes=next_slot_minutes)
    # 如果下一个整点在收盘后，强制改为14:50（末轮盯盘）
    target_end = now.replace(hour=TRADING_END.hour, minute=TRADING_END.minute, second=0, microsecond=0)
    if target > target_end:
        target = target_end
    wait_sec = (target - now).total_seconds()
    if wait_sec > 0:
        log.info(f"等待 {wait_sec / 60:.1f} 分钟到 {target.strftime('%H:%M')}")
        time.sleep(wait_sec)


def load_config() -> Dict:
    """加载持仓配置"""
    if CONFIG_PATH.exists():
        for enc in ("utf-8", "utf-8-sig", "gbk", "cp936"):
            try:
                with open(CONFIG_PATH, "r", encoding=enc) as f:
                    return json.load(f)
            except UnicodeDecodeError:
                continue
        with open(CONFIG_PATH, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    else:
        log.error(f"配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)


def build_holdings_list(holdings: List[Dict], exclude_symbol: str) -> List[Dict]:
    """构建 holdings 列表（排除当前分析的标的）"""
    return [
        {
            "symbol": h["symbol"],
            "name": h["name"],
            "weight_pct": h["position_pct"],
            "cost": h["cost"],
        }
        for h in holdings
        if h["symbol"] != exclude_symbol
    ]


def _sync_config_account_fields(config: Dict, account_stocks: List[Dict]) -> Dict:
    """Return a monitor config view backed by real-account holdings."""
    updated = dict(config)
    by_symbol = {s["symbol"]: s for s in account_stocks}
    updated["holdings"] = [
        by_symbol.get(s.get("symbol"), s)
        for s in config.get("holdings", [])
    ]
    updated["watchlist"] = [
        by_symbol.get(s.get("symbol"), s)
        for s in config.get("watchlist", [])
    ]
    return updated


def run_monitor_round():
    """执行一轮监控"""
    round_dt = datetime.now()
    round_time = round_dt.strftime("%H:%M")
    log.info(f"=== 开始监控轮次 {round_time} ===")

    config = load_config()
    try:
        from db.account_ledger import AccountLedger, REAL_ACCOUNT
        ledger = AccountLedger()
        ledger.ensure_initialized(config)
        account_stocks = ledger.stocks_for_committee_input(config, account=REAL_ACCOUNT)
        config = _sync_config_account_fields(config, account_stocks)
    except Exception as e:
        ledger = None
        log.warning(f"双账户账本初始化/读取失败，回退配置持仓: {e}")

    holdings = config.get("holdings", [])
    watchlist = config.get("watchlist", [])
    total_assets = config["total_assets"]
    cash = config["cash"]
    if ledger is not None:
        try:
            real_summary = ledger.account_summary("real")
            cash = float(real_summary.get("cash_cny", cash) or cash)
        except Exception:
            pass

    all_stocks = holdings + watchlist
    all_symbols = [s["symbol"] for s in all_stocks]
    entry_exit_state_before_round = load_entry_exit_alert_state()

    # 1. 拉取行情
    log.info(f"拉取 {len(all_symbols)} 只标的最新行情...")
    prices = fetch_sina_prices(all_symbols)
    log.info(f"获取到 {len(prices)} 只标的价格")

    # 1.5 拉取国内新闻
    news_items = []
    try:
        sys.path.insert(0, str(_PROJECT_ROOT))
        from services.news_sources import fetch_all
        news_items = fetch_all(domestic=True, max_per_source=8, timeout_sec=20)
        log.info(f"新闻: {len(news_items)} 条")
    except Exception as e:
        log.warning(f"新闻拉取失败: {e}")

    # 2. 逐只跑委员会
    results = []
    for stock in all_stocks:
        sym = stock["symbol"]
        price_info = prices.get(sym)

        if not price_info or price_info["price"] <= 0:
            log.warning(f"{sym} 行情缺失，跳过")
            results.append({"success": False, "symbol": sym, "name": stock["name"],
                            "error": "行情缺失"})
            continue

        # 构建 holdings（排除当前标的）
        other_holdings = build_holdings_list(holdings, sym)

        # 构建新闻摘要文本
        news_text = ""
        if news_items:
            try:
                from services.news_sources.domestic_hot_news import format_sector_brief
                news_text = format_sector_brief(news_items, max_items=8)
            except Exception:
                lines = []
                for item in news_items[:8]:
                    title = item.title[:100] if item.title else ""
                    if title:
                        lines.append(f"- [{item.src_name}] {title}")
                news_text = "\n".join(lines)

        result = call_committee(
            symbol=sym,
            name=stock["name"],
            market=stock.get("market", "a"),
            position_pct=stock.get("position_pct", 0),
            cost=stock.get("cost", 0),
            current_price=price_info["price"],
            total_assets=total_assets,
            cash=cash,
            all_holdings=other_holdings,
            target_position_pct=stock.get("target_position_pct", stock.get("target_pct")),
            t2_pending=config.get("t2_pending_cash", 0),
            news_brief=news_text,
            sector=stock.get("sector", ""),
            industry=stock.get("industry", ""),
            fundamentals=stock.get("fundamentals", {}),
            optimizer_review_enabled=config.get("optimizer_review_enabled", True),
        )
        if result and result.get("success"):
            result.setdefault("symbol", sym)
            result.setdefault("name", stock["name"])
            # 兜底 market：委员会响应理应回传 market，但若某生产端/旧响应漏了，
            # 用 config 里该标的的 market 补上，避免 _is_limit_up_buy_blocked 退化成 "a"
            # 把港股/美股误判 A 股涨停。
            result.setdefault("market", stock.get("market", "a"))
            result = apply_repeated_trade_guard(
                result,
                stock=stock,
                current_price=price_info["price"],
                ledger=ledger,
                entry_exit_state=entry_exit_state_before_round,
            )
            # 涨停追高护栏必须在下单前：改写 verdict→HOLD，使 apply_committee_result
            # 真正拦下影子买单（仅在 select_optimal_actionable_alerts 抑制提醒不够）。
            result = apply_limit_up_guard(result, price_info=price_info)
            if ledger is not None:
                try:
                    trade = ledger.apply_committee_result(result, price=price_info["price"])
                    if trade:
                        log.info(
                            f"影子账户执行 {trade.direction} {trade.symbol} "
                            f"{trade.units:.0f}股 @{trade.price:.2f}"
                        )
                    elif result.get("execution_blocked"):
                        log.info(
                            f"执行保护: {sym} {stock['name']} "
                            f"{result.get('optimizer_verdict_before_guard')} "
                            f"alloc=¥{result.get('optimizer_alloc_cny_before_guard', 0):,.0f} -> HOLD"
                        )
                except Exception as e:
                    log.warning(f"影子账户执行委员会建议失败 {sym}: {e}")
        results.append(result)

        # 打印简要结果
        if result.get("success"):
            v = result["verdict"]
            conf = result["confidence"]
            alloc = result.get("suggested_alloc_cny", 0)
            log.info(f"  {sym} {stock['name']}: {v} (conf={conf:.2f}, alloc=¥{alloc:,.0f})")
        else:
            log.warning(f"  {sym} {stock['name']}: 分析失败 - {result.get('error', 'unknown')}")

        # 避免过快连续请求
        time.sleep(1)

    # 3. 组合级提醒优化：现金预算 + 交易约束 + LLM/点位质量
    actionable, suppressed_alerts = select_optimal_actionable_alerts(
        results=results,
        prices=prices,
        cash=cash,
        stocks=all_stocks,
        entry_exit_state=entry_exit_state_before_round,
        max_alerts=int(config.get("max_action_alerts", 4) or 4),
        portfolio_value=total_assets,
        max_single_position_pct=float(config.get("max_single_position_pct", 25.0) or 25.0),
        max_sector_position_pct=float(config.get("max_sector_position_pct", 35.0) or 35.0),
    )

    entry_exit_alerts, entry_exit_watch = update_entry_exit_alert_state(
        results=results,
        prices=prices,
        holding_symbols={str(h.get("symbol", "")).upper() for h in holdings if h.get("symbol")},
    )

    window_snapshot = build_monitor_window_snapshot(
        round_time=round_time,
        results=results,
        actionable=actionable,
        prices=prices,
        stocks=all_stocks,
        entry_exit_watch=entry_exit_watch,
        entry_exit_alerts=entry_exit_alerts,
        suppressed_alerts=suppressed_alerts,
        cash=cash,
        total_assets=total_assets,
    )
    write_monitor_window_snapshot(window_snapshot)

    # 4. 写报告
    write_report(
        round_time,
        results,
        actionable,
        prices,
        news_items,
        entry_exit_watch=entry_exit_watch,
        entry_exit_alerts=entry_exit_alerts,
        suppressed_alerts=suppressed_alerts,
    )
    if ledger is not None:
        try:
            from db.account_ledger import prices_from_sina_result
            pnl_rows = ledger.snapshot_daily_pnl(prices=prices_from_sina_result(prices))
            for row in pnl_rows:
                log.info(
                    f"账户PnL {row['account']}: total={row['total_value_cny']:,.0f}, "
                    f"day={row['day_pnl_cny']}, pnl={row['total_pnl_pct']:+.2f}%"
                )
        except Exception as e:
            log.warning(f"双账户收盘/PnL快照失败: {e}")

    # 5. 通知
    if not MONITOR_POPUPS_ENABLED:
        log.info("稳定监控窗口模式：跳过弹框。设置 INVEST_MONITOR_POPUPS=1 可恢复弹框。")
        if actionable:
            log.info(f"需要操作 {len(actionable)} 只标的，已写入 {LATEST_WINDOW_PATH}")
        return

    # - 整点/半点/尾盘：允许综合摘要（可含新闻）
    # - 其他交易时间：只有真正需要操作时才弹综合摘要；新闻刷新本身不弹窗
    # - 买卖点连续触发：始终单独弹窗
    import re as _re
    scheduled_summary = is_scheduled_monitor_popup_time(round_dt)
    if should_send_monitor_summary_popup(now=round_dt, actionable=actionable):
        toast_lines = []

        # 新闻源摘要只在整点/半点/尾盘综合摘要中展示，交易日新闻刷新本身不触发弹窗。
        if scheduled_summary and news_items:
            toast_lines.append("── 📰 新闻 ──")
            for item in news_items[:3]:
                title = item.title[:60] if item.title else ""
                if title:
                    toast_lines.append(f"  [{item.src_name}] {title}")
                    sectors = (item.raw_meta or {}).get("sectors", [])
                    if sectors:
                        leaders = sectors[0].get("leaders", [])
                        leader = leaders[0] if leaders else {}
                        if leader.get("name"):
                            toast_lines.append(
                                f"    {sectors[0].get('sector')} → {leader.get('name')}({leader.get('symbol')})"
                            )

        # actionable 标的
        if actionable:
            toast_lines.append("── ⚠️ 需操作 ──")
            for a in actionable[:4]:
                # 入场出场点
                ee = a.get("entry_exit_points", {}) or {}
                buy = ee.get("buy_pullback_price") or ee.get("buy_breakout_price")
                sl_p = ee.get("stop_loss_price")
                tp_p = ee.get("take_profit_price")
                exit_p = ee.get("trim_price")
                rr = ee.get("reward_risk_ratio", 0)

                # LLM审核
                review = a.get("optimizer_review", "") or ""
                rl = _re.search(r"ONE_LINE:\s*(.+)", review) if _re else None
                comment = rl.group(1)[:60] if rl else ""

                # 手数（买卖都按最小交易单位估算）
                alloc = a.get("suggested_alloc_cny", 0)
                sym = a.get("symbol", "")
                price = prices.get(sym, {}).get("price", 1)
                stock_cfg = next((s for s in all_stocks if s.get("symbol") == sym), {})
                lot = int(stock_cfg.get("min_lot_size") or 100)
                hands = int(abs(alloc) / (price * lot)) if price > 0 and abs(alloc) > 0 and lot > 0 else 0
                hands_str = f" {hands}手" if hands > 0 else ""

                current_shares = 0
                if price > 0:
                    current_shares = int(
                        stock_cfg.get("position_pct", 0) / 100 * config["total_assets"] / price
                    )
                current_hands = int(current_shares / lot) if lot > 0 else 0
                remain_hands = max(0, current_hands - hands) if alloc < 0 else current_hands
                remain_str = f" ->剩{remain_hands}手" if alloc < 0 and current_hands > 0 else ""

                v_map = {"ACCUMULATE": "🟢↑买", "BUY": "🟢↑买", "TRIM": "🔴↓卖", "SELL": "🔴↓卖"}
                v_icon = v_map.get(a['verdict'], a['verdict'])

                parts = [f"  {v_icon} {a['name']}{hands_str}{remain_str} ¥{alloc:,.0f}"]
                if buy: parts.append(f"买¥{buy:.2f}")
                if sl_p: parts.append(f"损¥{sl_p:.2f}")
                if tp_p: parts.append(f"盈¥{tp_p:.2f}")
                if exit_p: parts.append(f"出¥{exit_p:.2f}")
                if rr: parts.append(f"R{rr:.1f}")
                parts.append(f"(c={a['confidence']:.2f})")
                toast_lines.append(" ".join(parts))
                if comment:
                    toast_lines.append(f"    💬 {comment}")
        else:
            toast_lines.append("── ✅ 无需操作 ──")

        toast_body = "\n".join(toast_lines[:25])  # 最多25行
        send_windows_toast(
            f"📊 {round_time} 监控 ({len(actionable)}只需操作)",
            toast_body
        )
    else:
        log.info("跳过综合弹窗：非整点/半点/尾盘，且无交易操作提醒")
    if entry_exit_alerts:
        send_windows_toast(
            f"🚨 买卖点连续触发 ({len(entry_exit_alerts)}只)",
            format_entry_exit_alert_body(entry_exit_alerts),
        )
        log.info(f"🚨 买卖点连续触发: {len(entry_exit_alerts)} 只标的")
    if actionable:
        log.info(f"⚠️ 需要操作: {len(actionable)} 只标的")
    else:
        log.info("✅ 本轮无需操作")


def main():
    """主入口：循环执行监控"""
    log.info("=== OpenInvest 盘中监控启动 ===")
    log.info(f"交易时间: {TRADING_START}-{LUNCH_START}, {LUNCH_END}-{TRADING_END}, 间隔: {INTERVAL_MINUTES}分钟")
    log.info("委员会分析: 直接 Python 调用（无需后端端口）")

    # 立即执行第一轮
    if is_trading_time():
        log.info("当前在交易时段，立即执行首轮...")
        try:
            run_monitor_round()
        except Exception as e:
            log.exception(f"首轮执行失败: {e}")
    else:
        log.info("当前非交易时段，等待开盘...")

    # 主循环
    while True:
        try:
            if not is_trading_time():
                # 非交易时段，每分钟检查一次
                now = datetime.now()
                if now.weekday() >= 5:
                    # 周末，等到周一
                    days_to_monday = 7 - now.weekday()
                    log.info(f"周末，等待 {days_to_monday} 天到周一...")
                    time.sleep(3600)
                else:
                    log.info(f"非交易时段 ({now.strftime('%H:%M')})，等待中...")
                    time.sleep(60)
                continue

            # 交易时段：等待到下一个监控间隔节点
            wait_until_next_round()
            run_monitor_round()

        except KeyboardInterrupt:
            log.info("用户中断，退出监控")
            break
        except Exception as e:
            log.exception(f"监控轮次异常: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
