"""Notification timing, Windows toast, and action-email helpers for market monitor."""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from jobs.market_monitor_common import ACTION_EMAIL_STATE_PATH, TRADING_END, log, _fmt_price, _safe_num

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


def _action_direction(action: Dict[str, Any]) -> str:
    verdict = str(action.get("verdict") or "").upper()
    alloc = _safe_num(action.get("suggested_alloc_cny"))
    if verdict in {"SELL", "TRIM"} or alloc < 0:
        return "SELL"
    return "BUY"


def _action_lots(action: Dict[str, Any], *, prices: Dict[str, Dict[str, Any]], stock: Dict[str, Any]) -> int:
    explicit = int(_safe_num(action.get("alert_selected_lots") or action.get("optimizer_lots")))
    if explicit > 0:
        return explicit
    symbol = str(action.get("symbol") or "").upper()
    price = _safe_num((prices.get(symbol) or {}).get("price"))
    lot = max(1, int(_safe_num(stock.get("min_lot_size") or action.get("min_lot_size"), 100) or 100))
    alloc = abs(_safe_num(action.get("suggested_alloc_cny")))
    if price <= 0 or alloc <= 0:
        return 0
    return int(alloc // (price * lot))


def _trigger_summary(action: Dict[str, Any]) -> str:
    triggers = action.get("alert_triggers") or []
    if not triggers and action.get("discipline_review"):
        review = action.get("discipline_review") or {}
        kind = review.get("trigger_kind")
        if kind:
            return str(kind)
    parts = []
    for trigger in triggers:
        kind = str(trigger.get("kind") or trigger.get("side") or "")
        level = _safe_num(trigger.get("level"))
        parts.append(f"{kind}@{_fmt_price(level)}" if level > 0 else kind)
    return ", ".join(p for p in parts if p) or "-"


def _review_one_line(action: Dict[str, Any]) -> str:
    review = str(action.get("optimizer_review") or "")
    for line in review.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("ONE_LINE:"):
            return stripped.split(":", 1)[1].strip()
    synthesis = action.get("decision_synthesis") or {}
    if synthesis.get("explanation"):
        return str(synthesis.get("explanation"))
    memo = str(action.get("cio_memo") or "")
    return memo.strip().splitlines()[0][:180] if memo.strip() else ""


def _action_email_key(action: Dict[str, Any], *, lots: int, trigger_text: str, trade_date: str) -> str:
    symbol = str(action.get("symbol") or "").upper()
    direction = _action_direction(action)
    alert_source = str(action.get("alert_source") or "")
    review = action.get("discipline_review") or {}
    trigger_kind = str(review.get("trigger_kind") or trigger_text or "")
    return "|".join([trade_date, symbol, direction, str(lots), alert_source, trigger_kind])


def _load_action_email_state(path: Optional[Any] = None) -> Dict[str, Any]:
    path = path or ACTION_EMAIL_STATE_PATH
    if not path.exists():
        return {"version": 1, "sent": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": 1, "sent": {}}
        data.setdefault("version", 1)
        data.setdefault("sent", {})
        return data
    except Exception:
        return {"version": 1, "sent": {}}


def _save_action_email_state(state: Dict[str, Any], path: Optional[Any] = None) -> None:
    path = path or ACTION_EMAIL_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _format_action_email_markdown(
    *,
    round_time: str,
    actionable: List[Dict[str, Any]],
    prices: Dict[str, Dict[str, Any]],
    stocks: List[Dict[str, Any]],
    trade_date: str,
) -> tuple[str, List[str]]:
    stock_by_symbol = {str(s.get("symbol") or "").upper(): s for s in stocks}
    lines = [
        f"# OpenInvest 执行提醒",
        "",
        f"- 时间: {trade_date} {round_time}",
        f"- 需要执行: {len(actionable)} 个标的",
        "",
        "| 标的 | 方向 | 手数 | 现价 | 触发/来源 | 金额 | 原因 |",
        "|---|---|---:|---:|---|---:|---|",
    ]
    keys: List[str] = []
    for action in actionable:
        symbol = str(action.get("symbol") or "").upper()
        stock = stock_by_symbol.get(symbol, {})
        name = action.get("name") or stock.get("name") or symbol
        direction = _action_direction(action)
        lots = _action_lots(action, prices=prices, stock=stock)
        llm_lots = int(_safe_num(action.get("llm_review_lots")))
        lots_text = f"{lots}手"
        if llm_lots > 0 and llm_lots != lots:
            lots_text += f"（LLM审核推荐{llm_lots}手）"
        price = _safe_num((prices.get(symbol) or {}).get("price"))
        trigger_text = _trigger_summary(action)
        source = action.get("alert_source") or action.get("reason") or "-"
        alloc = _safe_num(action.get("suggested_alloc_cny"))
        reason = _review_one_line(action).replace("|", "/")[:140] or str(source)
        direction_label = "买入" if direction == "BUY" else "卖出"
        lines.append(
            f"| {name} ({symbol}) | {direction_label} | {lots_text} | {_fmt_price(price)} | "
            f"{trigger_text} / {source} | {alloc:,.0f} | {reason} |"
        )
        keys.append(_action_email_key(action, lots=lots, trigger_text=trigger_text, trade_date=trade_date))
    lines.extend([
        "",
        "说明：邮件只在主窗口出现 `action_required` 时发送；同一交易日相同标的、方向、手数和触发来源只发送一次。",
    ])
    return "\n".join(lines), keys


def send_action_required_email(
    *,
    round_time: str,
    actionable: List[Dict[str, Any]],
    prices: Dict[str, Dict[str, Any]],
    stocks: List[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> str:
    """Send one deduplicated email when actionable monitor alerts appear.

    Disabled unless ``INVEST_MONITOR_EMAIL_ACTIONS=1``. Missing SMTP credentials
    return ``""`` through services.notifier, matching existing report emails.
    """
    if not actionable:
        return ""
    if os.getenv("INVEST_MONITOR_EMAIL_ACTIONS", "0") != "1":
        return ""
    now = now or datetime.now()
    trade_date = now.date().isoformat()
    md, keys = _format_action_email_markdown(
        round_time=round_time,
        actionable=actionable,
        prices=prices,
        stocks=stocks,
        trade_date=trade_date,
    )
    state = _load_action_email_state()
    sent = state.setdefault("sent", {})
    day_sent = sent.setdefault(trade_date, {})
    unsent_keys = [key for key in keys if key not in day_sent]
    if not unsent_keys:
        log.info("执行提醒邮件跳过：本交易日相同触发已发送")
        return ""

    from services.notifier import render_markdown_email, send_email_html

    subject = f"OpenInvest 执行提醒：{len(actionable)} 个标的需要操作"
    receiver = send_email_html(
        subject=subject,
        html_body=render_markdown_email(md, footer_label="OpenInvest Monitor"),
        plain_body=md,
    )
    if receiver:
        stamp = now.isoformat(timespec="seconds")
        for key in unsent_keys:
            day_sent[key] = stamp
        # Keep the state compact: only current day matters for dedupe.
        state["sent"] = {trade_date: day_sent}
        _save_action_email_state(state)
        log.info(f"执行提醒邮件已发送: {receiver}")
    return receiver
