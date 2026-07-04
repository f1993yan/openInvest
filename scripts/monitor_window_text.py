"""Formatting and beginner-friendly explanation helpers for the desktop monitor window."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from core.buy_signal_miner import buy_signal_summary_text
from scripts.monitor_window_constants import (
    ACTION_BG,
    ACTION_FG,
    BLUE,
    CARD_BG,
    DOWN_FG,
    MUTED,
    STATE_LABELS,
    STATE_SIGNALS,
    TRIGGER_BG,
    TRIGGER_FG,
    UP_FG,
    VERDICT_ARROWS,
)
from scripts.monitor_window_services import (
    _fmt_lots,
    _fmt_money,
    _fmt_pct,
    _fmt_price,
    _safe_num,
    _short,
    _suggested_lots,
)

def _label_state(state: Any) -> str:
    return STATE_LABELS.get(str(state or ""), str(state or "-"))


def _state_signal(state: Any) -> str:
    state_key = str(state or "")
    return STATE_SIGNALS.get(state_key, "-")


def _verdict_signal(verdict: Any) -> str:
    verdict_key = str(verdict or "").upper()
    return VERDICT_ARROWS.get(verdict_key, "·")


def _row_tag(row: Dict[str, Any]) -> str:
    state = str(row.get("state") or "")
    op = row.get("operation") or {}
    verdict = str(op.get("verdict") or "").upper()
    alloc = _safe_num(op.get("suggested_alloc_cny"))
    change = _safe_num((row.get("price") or {}).get("change_pct"))
    if state == "candidate" and (verdict in {"SELL", "TRIM"} or alloc < 0):
        return "trigger"
    if state == "action_required":
        return "action"
    if state in {"trigger_confirmed", "watch_trigger"}:
        return "trigger"
    if state == "error":
        return "error"
    if state == "blocked":
        return "blocked"
    if state == "executed":
        return "blocked"
    if change > 0:
        return "up"
    if change < 0:
        return "down"
    return "neutral"


def _state_color(row: Dict[str, Any]) -> str:
    tag = _row_tag(row)
    if tag == "action":
        return ACTION_FG
    if tag == "trigger":
        return TRIGGER_FG
    if tag == "up":
        return UP_FG
    if tag in {"down", "error"}:
        return DOWN_FG
    if tag == "blocked":
        return MUTED
    return BLUE


def _card_bg(row: Dict[str, Any]) -> str:
    tag = _row_tag(row)
    if tag == "action":
        return ACTION_BG
    if tag == "trigger":
        return TRIGGER_BG
    return CARD_BG


def _stack_layer_bg(row: Dict[str, Any]) -> str:
    state = str(row.get("state") or "")
    if state in {"action_required", "error"}:
        return "#ffd8d2"
    if state in {"trigger_confirmed", "watch_trigger"}:
        return "#ffe4b5"
    if state == "blocked":
        return "#d7dde7"
    return "#d7e8ff"


def _change_color(row: Dict[str, Any]) -> str:
    change = _safe_num((row.get("price") or {}).get("change_pct"))
    if change > 0:
        return UP_FG
    if change < 0:
        return DOWN_FG
    return MUTED


def _buy_summary(row: Dict[str, Any]) -> str:
    buy = row.get("buy_criteria") or {}
    parts = []
    pullback = _safe_num(buy.get("pullback_price"))
    breakout = _safe_num(buy.get("breakout_price"))
    rr = _safe_num(buy.get("reward_risk_ratio"))
    if pullback > 0:
        parts.append(f"回{pullback:.2f}")
    if breakout > 0:
        parts.append(f"突{breakout:.2f}")
    if rr > 0:
        parts.append(f"盈亏{rr:.1f}")
    return " ".join(parts[:3]) or "-"


def _exit_summary(row: Dict[str, Any]) -> str:
    exit_points = row.get("exit_points") or {}
    parts = []
    stop = _safe_num(exit_points.get("stop_loss_price"))
    take = _safe_num(exit_points.get("take_profit_price"))
    prefix = "纪" if exit_points.get("plan_type") == "a_share_position_exit" else ""
    if stop > 0:
        parts.append(f"{prefix}损{stop:.2f}")
    if take > 0:
        parts.append(f"止{take:.2f}")
    return " ".join(parts[:2]) or "-"


def _sector_summary(row: Dict[str, Any]) -> str:
    op = row.get("operation") or {}
    discipline = op.get("discipline_review") or {}
    panic_guard = discipline.get("sector_panic_guard") or {}
    sector = str(panic_guard.get("sector") or row.get("sector") or row.get("industry") or "").strip()
    if not sector:
        return "板块 -"
    source = "恐慌判定" if panic_guard.get("sector") else "分类"
    return f"板块 {_short(sector, 12)}（{source}）"


def _operation_summary(row: Dict[str, Any]) -> str:
    op = row.get("operation") or {}
    status = str(op.get("status") or row.get("state") or "")
    alloc = _safe_num(op.get("suggested_alloc_cny"))
    lots = _suggested_lots(row)
    verdict = str(op.get("verdict") or "").upper()
    discipline = op.get("discipline_review") or {}
    trigger_kind = str(discipline.get("trigger_kind") or "")
    side = "卖" if verdict in {"SELL", "TRIM"} or alloc < 0 or lots < 0 else "买"
    if status == "action_required":
        if trigger_kind in {"position_stop", "cost_stop_loss", "stop_loss"}:
            return f"止损卖{_fmt_lots(abs(lots))}" if lots else "止损卖"
        if trigger_kind in {"take_profit_2", "take_profit", "take_profit_1", "trim"}:
            return f"止盈卖{_fmt_lots(abs(lots))}" if lots else "止盈卖"
        return f"{side}{_fmt_lots(abs(lots))}" if lots else "操作"
    if status in {"trigger_confirmed", "watch_trigger"}:
        if trigger_kind in {"position_stop", "cost_stop_loss", "stop_loss"}:
            return "止损复核"
        if trigger_kind in {"take_profit_2", "take_profit", "take_profit_1", "trim"}:
            return "止盈复核"
        return "触发"
    if status == "candidate":
        if discipline:
            if trigger_kind in {"position_stop", "cost_stop_loss", "stop_loss"}:
                return "止损复核"
            return "止盈复核"
        if verdict in {"SELL", "TRIM"} or alloc < 0:
            return "待确认卖"
        if verdict in {"BUY", "ACCUMULATE"} or alloc > 0:
            return "候选买"
        return "候选"
    if status == "blocked":
        return "拦截"
    if status == "executed":
        return "已执行"
    return "观察"


def _llm_review_lots_hint(row: Dict[str, Any]) -> str:
    op = row.get("operation") or {}
    if str(op.get("status") or row.get("state") or "") != "action_required":
        return ""
    optimizer_lots = int(_safe_num(op.get("optimizer_lots"), _safe_num(op.get("alert_selected_lots"))))
    llm_lots = int(_safe_num(op.get("llm_review_lots"), optimizer_lots))
    if optimizer_lots <= 0 or llm_lots == optimizer_lots:
        return ""
    return f"LLM审核推荐{llm_lots}手"


def _verdict_label(verdict: Any) -> str:
    return {
        "BUY": "买入",
        "ACCUMULATE": "小幅加仓",
        "HOLD": "持有不动",
        "WAIT": "等待",
        "TRIM": "减仓",
        "SELL": "卖出",
    }.get(str(verdict or "").upper(), "暂无明确结论")


def _compact_verdict_label(verdict: Any) -> str:
    return {
        "BUY": "买入",
        "ACCUMULATE": "加仓",
        "HOLD": "持有",
        "WAIT": "等待",
        "TRIM": "减仓",
        "SELL": "卖出",
    }.get(str(verdict or "").upper(), "暂无")


def _regime_label(text: Any) -> str:
    value = str(text or "")
    match = re.search(r"REGIME:\s*([a-z_]+)", value, re.I)
    regime = match.group(1).lower() if match else ""
    label = {
        "uptrend": "上升趋势",
        "downtrend": "下跌趋势",
        "range_bound": "震荡",
        "crash": "急跌",
        "recovery": "修复",
    }.get(regime, "趋势不明")
    reason_match = re.search(r"REASON:\s*([^\n]+)", value)
    reason = reason_match.group(1).strip() if reason_match else ""
    return f"{label}：{reason}" if reason else label


def _extract_one_line(text: Any) -> str:
    value = str(text or "").strip()
    for key in ("ONE_LINE:", "HUMAN_CHECK:", "RATIONALE:"):
        match = re.search(rf"{key}\s*(.+?)(?=\s+[A-Z_]+:|$)", value, re.S)
        if match:
            return _short(match.group(1).strip(), 300)
    return _short(value, 300) if value else "-"


def _extract_risk_flags(text: Any) -> List[str]:
    value = str(text or "")
    match = re.search(r"RISK_FLAGS:\s*(.+?)(?=\s+[A-Z_]+:|$)", value, re.S)
    if not match:
        return []
    return [
        item.strip(" ，,;；")
        for item in re.split(r"[,，;；]", match.group(1))
        if item.strip(" ，,;；")
    ][:3]


def _distance_pct(current: Any, target: Any) -> Optional[float]:
    current_num = _safe_num(current)
    target_num = _safe_num(target)
    if current_num <= 0 or target_num <= 0:
        return None
    return (target_num - current_num) / current_num * 100.0


def _fmt_distance(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if abs(value) < 0.05:
        return "几乎就在当前价"
    direction = "上方" if value > 0 else "下方"
    return f"{abs(value):.1f}% {direction}"


def _entry_exit_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    buy = row.get("buy_criteria") or {}
    exit_points = row.get("exit_points") or {}
    return {
        "buy_pullback_price": buy.get("pullback_price"),
        "buy_breakout_price": buy.get("breakout_price"),
        "stop_loss_price": exit_points.get("stop_loss_price"),
        "take_profit_price": exit_points.get("take_profit_price"),
        "trim_price": exit_points.get("trim_price"),
    }


def _review_text_from_row(row: Dict[str, Any]) -> str:
    llm = row.get("llm_review") or {}
    return str(llm.get("raw_excerpt") or llm.get("one_line") or llm.get("conclusion") or "")


def _detail_line_style(line: str) -> str:
    """Return a semantic style tag for monitor detail text lines."""
    text = str(line or "").strip()
    if not text:
        return "normal"
    lower = text.lower()
    risk_markers = (
        "风险",
        "止损",
        "跌破",
        "破位",
        "下行",
        "回撤",
        "亏损",
        "负预期",
        "负收益",
        "卖出",
        "减仓",
        "降低风险",
        "不通过",
        "未通过",
        "偏弱",
        "低分",
        "空头",
        "bearish",
        "downtrend",
        "crash",
        "risk_flags",
        "需要小心",
    )
    positive_markers = (
        "加分",
        "正向",
        "通过",
        "偏强",
        "上升趋势",
        "买入",
        "加仓",
        "突破",
        "收益为正",
        "成功率",
        "胜率",
        "优势",
        "支撑",
        "bullish",
        "uptrend",
    )
    muted_markers = (
        "已用刚刚",
        "下面是上次",
        "当前价:",
        "理想买点:",
        "仓位建议:",
        "持仓纪律",
        "入场估算",
        "缠论与技术形态分析",
        "最新确认笔",
        "最新中枢状态",
        "形态警示",
    )
    if any(marker in text or marker in lower for marker in risk_markers):
        return "risk"
    if any(marker in text or marker in lower for marker in positive_markers):
        return "positive"
    if text.endswith(":") or text.startswith(("结论", "为什么", "下一步", "一致性检查")):
        return "heading"
    if text.startswith(("1.", "2.", "3.", "4.", "- 技术面", "- 基本面", "- 模型提醒")) or any(
        marker in text for marker in muted_markers
    ):
        return "muted"
    return "normal"


def _beginner_summary_lines(
    row: Dict[str, Any],
    *,
    result: Optional[Dict[str, Any]] = None,
) -> List[str]:
    source = result if result is not None and result.get("success") else None
    op = row.get("operation") or {}
    price = row.get("price") or {}
    fundamental = row.get("fundamental") or {}
    technical = row.get("technical") or {}
    current = _safe_num(price.get("current"))
    verdict = (source or op).get("verdict")
    confidence = _safe_num((source or op).get("confidence"))
    alloc = _safe_num((source or op).get("suggested_alloc_cny"))
    alloc_forced_side = ""
    if alloc < 0 and str(verdict or "").upper() in {"", "HOLD", "WAIT"}:
        verdict = "TRIM"
        alloc_forced_side = "sell"
    elif alloc > 0 and str(verdict or "").upper() in {"", "HOLD", "WAIT"}:
        verdict = "ACCUMULATE"
        alloc_forced_side = "buy"
    ee = (source or {}).get("entry_exit_points") or _entry_exit_from_row(row)
    right_gate = (source or {}).get("right_side_trend_gate") or row.get("right_side_trend_gate") or {}
    review = str((source or {}).get("optimizer_review") or (source or {}).get("cio_memo") or _review_text_from_row(row))
    decision_synthesis = (source or {}).get("decision_synthesis") or row.get("decision_synthesis") or {}
    discipline_review = op.get("discipline_review") or {}
    buy_signal_backtest = (
        (source or {}).get("buy_signal_backtest")
        or row.get("buy_signal_backtest")
        or (row.get("technical") or {}).get("buy_signal_backtest")
        or {}
    )
    one_line = _extract_one_line(review)
    risk_flags = _extract_risk_flags(review)
    regime_text = (source or {}).get("regime") or technical.get("regime") or technical.get("quant_view")
    pullback = ee.get("buy_pullback_price")
    breakout = ee.get("buy_breakout_price")
    exit_points = row.get("exit_points") or {}
    stop = exit_points.get("stop_loss_price")
    if not stop or stop <= 0:
        stop = ee.get("stop_loss_price")
    take = exit_points.get("take_profit_price")
    if not take or take <= 0:
        take = ee.get("take_profit_price")
    plan_type = str(exit_points.get("plan_type") or "")
    pullback_dist = _distance_pct(current, pullback)
    breakout_dist = _distance_pct(current, breakout)
    stop_dist = _distance_pct(current, stop)
    take_dist = _distance_pct(current, take)
    if source:
        header = "结论（最新）"
        status_note = "已用刚刚跑完的委员会结果更新。"
    else:
        header = "结论（快照）"
        status_note = "下面是上次监控快照；最新分析完成后会自动刷新。"
    action = _verdict_label(verdict)
    if decision_synthesis:
        action = str(decision_synthesis.get("action_label") or action)
        primary_reason = str(decision_synthesis.get("primary_reason") or "")
        decision = f"{action}，{primary_reason}" if primary_reason else f"{action}。"
    elif alloc_forced_side == "sell":
        decision = "待确认卖出，仓位建议为负数，但旧结果里 verdict 仍是 HOLD；请以最新重新分析后的 verdict 为准。"
    elif alloc_forced_side == "buy":
        decision = "候选买入，仓位建议为正数，但仍要核对买点和现金约束。"
    elif str(verdict or "").upper() in {"BUY", "ACCUMULATE"} and current > 0 and _safe_num(pullback) > 0 and current > _safe_num(pullback) * 1.03:
        decision = f"{action}，但当前价离回调买点偏高，别急着追。"
    elif str(verdict or "").upper() in {"BUY", "ACCUMULATE"}:
        decision = f"{action}，先看是否接近买点。"
    elif str(verdict or "").upper() in {"TRIM", "SELL"}:
        decision = f"{action}，重点看是否触发止损/减仓线。"
    elif discipline_review:
        edge = _safe_num(discipline_review.get("expected_utility_edge_pct"))
        decision = f"纪律复核，卖出期望相对继续持有净优势 {edge:.2f}pct。"
    else:
        decision = f"{action}，暂时以观察为主。"
    lines = [
        f"{header}: {decision}",
        status_note,
        "",
        "你最该先看这几项:",
        f"1. 当前价: {_fmt_price(current)}，今日涨跌 {_fmt_pct(price.get('change_pct'))}。",
        f"   所属板块: {str(row.get('sector') or row.get('industry') or '-')}。",
        f"2. 理想买点: 回调 {_fmt_price(pullback)}（在当前价 {_fmt_distance(pullback_dist)}）；突破 {_fmt_price(breakout)}（在当前价 {_fmt_distance(breakout_dist)}）。",
        f"3. 风险线: {'持仓纪律' if plan_type == 'a_share_position_exit' else '入场估算'}，止损 {_fmt_price(stop)}（在当前价 {_fmt_distance(stop_dist)}）；止盈 {_fmt_price(take)}（在当前价 {_fmt_distance(take_dist)}）。",
        f"4. 仓位建议: {_fmt_money(alloc)} 元；置信度 {confidence:.0%}；当前{'已有持仓' if row.get('is_holding') else '没有持仓'}。",
        "",
        "为什么这么判断:",
        f"- 技术面: {_regime_label(regime_text)}",
        f"- 右侧趋势闸门: {'通过' if right_gate.get('allow') else '未通过'}"
        f"（{right_gate.get('reason') or '未提供'}）。",
        f"- 基本面: {_safe_num((source or {}).get('fundamental_score'), _safe_num(fundamental.get('score'), 50)):.0f} 分，属于{'偏强' if _safe_num((source or {}).get('fundamental_score'), _safe_num(fundamental.get('score'), 50)) >= 70 else '一般' if _safe_num((source or {}).get('fundamental_score'), _safe_num(fundamental.get('score'), 50)) >= 45 else '偏弱'}。",
    ]
    if decision_synthesis:
        for item in (decision_synthesis.get("evidence") or [])[:3]:
            lines.append(f"- 决策证据: {item}")
        for item in (decision_synthesis.get("conflicts") or [])[:2]:
            lines.append(f"- 口径冲突: {item}；最终按确定性优化器执行。")
    if discipline_review:
        trigger_label = {
            "position_stop": "持仓止损",
            "cost_stop_loss": "成本止损",
            "stop_loss": "技术止损",
            "take_profit_1": "第一止盈",
            "take_profit_2": "第二止盈",
            "take_profit": "止盈",
            "trim": "减仓",
        }.get(str(discipline_review.get("trigger_kind") or ""), "持仓纪律")
        lines.append(
            f"- 纪律复核: {trigger_label}已触发；"
            f"卖出期望 {_safe_num(discipline_review.get('sell_expected_edge_pct')):.2f}pct，"
            f"继续持有证据 {_safe_num(discipline_review.get('continuation_edge_pct')):.2f}pct，"
            f"净效用 {_safe_num(discipline_review.get('expected_utility_edge_pct')):.2f}pct。"
        )
        lines.append(
            f"- 参数可信度: 周度策略可靠性 {_safe_num(discipline_review.get('policy_reliability')):.0%}，"
            f"卖出胜率下界 {_safe_num(discipline_review.get('sell_win_rate_lower')):.0%}，"
            f"卖出后路径优势下界 {_safe_num(discipline_review.get('post_sell_positive_edge_lower')):.0%}。"
        )
        panic_guard = discipline_review.get("sector_panic_guard") or {}
        if panic_guard.get("active"):
            lines.append(
                f"- 板块恐慌保护: {panic_guard.get('sector') or '同板块'}中位跌幅"
                f" {_safe_num(panic_guard.get('sector_median_change_pct')):.2f}%，"
                f"下跌占比 {_safe_num(panic_guard.get('sector_down_ratio')):.0%}；"
                f"该股相对板块Z值 {_safe_num(panic_guard.get('target_relative_z')):.2f}，"
                f"不是独立走弱，已增加继续持有证据"
                f" {_safe_num(panic_guard.get('hold_utility_bonus_pct')):.2f}pct。"
            )
    if buy_signal_backtest:
        lines.append(f"- {buy_signal_summary_text(buy_signal_backtest)}")
    if one_line and one_line != "-":
        lines.append(f"- 模型提醒: {one_line}")
    synthesis_warnings = list((decision_synthesis.get("risk_warnings") or [])[:3]) if decision_synthesis else []
    signal_warning = str((buy_signal_backtest or {}).get("warning") or "")
    if risk_flags or synthesis_warnings or signal_warning:
        lines.extend(["", "需要小心:"])
        if plan_type == "a_share_position_exit":
            lines.append("- 已持仓标的的止盈止损盘中不重算，只在收盘后按追踪规则上移风险线。")
        for warning in synthesis_warnings:
            lines.append(f"- {warning}")
        if signal_warning:
            lines.append(f"- {signal_warning}")
        for flag in risk_flags:
            lines.append(f"- {flag}")
    lines.extend(
        [
            "",
            "下一步:",
            _beginner_next_step(
                verdict,
                current,
                pullback,
                breakout,
                stop,
                row=row,
                result=source,
            ),
        ]
    )
    if source is not None and not source.get("success"):
        lines.extend(["", f"分析失败: {source.get('error') or '-'}"])

    # Append Chan Theory & Pattern Analysis [SHADOW MODE]
    symbol = row.get("symbol")
    if symbol:
        try:
            from utils.market_data_provider import get_history_data
            df = get_history_data(symbol, "2y")
            if df is not None and not df.empty:
                from utils.chan import analyze_chan, format_chan_brief
                chan_data = analyze_chan(df, symbol)
                chan_brief = format_chan_brief(chan_data)
                if chan_brief:
                    lines.extend([
                        "",
                        "--- 缠论与技术形态分析 [影子模式] ---",
                        chan_brief
                    ])
        except Exception as e:
            import logging as _logging
            _logging.getLogger(__name__).warning(f"Failed to calculate Chan brief in monitor_window_text for {symbol}: {e}")

    return lines


def _beginner_next_step(
    verdict: Any,
    current: Any,
    pullback: Any,
    breakout: Any,
    stop: Any,
    *,
    row: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
) -> str:
    verdict_key = str(verdict or "").upper()
    current_num = _safe_num(current)
    pullback_num = _safe_num(pullback)
    breakout_num = _safe_num(breakout)
    stop_num = _safe_num(stop)
    op = (row or {}).get("operation") or {}
    status = str(op.get("status") or (row or {}).get("state") or "")
    wait_reasons = op.get("wait_reasons") or (row or {}).get("suppressed_reasons") or []
    if verdict_key in {"BUY", "ACCUMULATE"}:
        if pullback_num > 0 and current_num > pullback_num * 1.03:
            return f"先等回调接近 {_fmt_price(pullback_num)}，或放量站上 {_fmt_price(breakout_num)} 后再看；当前不适合盲目追高。"
        return "如果价格仍在买点附近，才考虑小仓位；买入前先确认能接受止损线。"
    if verdict_key in {"TRIM", "SELL"}:
        if status == "action_required":
            lots = abs(_suggested_lots(row or {}))
            return f"当前已进入卖出提醒，优先按最新价格核对并执行{'约 ' + _fmt_lots(lots) if lots else '减仓'}；不要等盘中动态止损反复变化。"
        if wait_reasons:
            return f"方向偏卖，但暂未进入执行提醒：{wait_reasons[0]}。若跌破 {_fmt_price(stop_num)} 或下一轮评分增强，再优先降风险。"
        return f"方向偏卖，重点看是否跌破 {_fmt_price(stop_num)}，或卖出期望收益是否继续占优。"
    discipline = op.get("discipline_review") or {}
    if discipline:
        edge = _safe_num(discipline.get("expected_utility_edge_pct"))
        if status == "action_required":
            lots = abs(_suggested_lots(row or {}))
            return f"持仓纪律已确认且净效用为正，按最新价核对后优先卖出{'约 ' + _fmt_lots(lots) if lots else '对应仓位'}。"
        return f"纪律线已经触发但净效用 {edge:.2f}pct 尚未压过继续持有证据，下一轮仍触发或净效用转正再执行。"
    return "先不动，等价格接近买点/止损点，或下一轮监控给出明确触发。"


def _path_window(path: Dict[str, Any], horizon_days: int) -> Dict[str, Any]:
    for item in path.get("windows") or []:
        if int(_safe_num(item.get("horizon_days"))) == horizon_days:
            return item
    return {}


def _risk_level_label(value: Any) -> str:
    return {
        "low": "低",
        "medium": "中",
        "high": "高",
        "unknown": "未知",
    }.get(str(value or "").lower(), str(value or "-"))


def _path_plain_text(path: Dict[str, Any], risk: Dict[str, Any]) -> tuple[str, bool]:
    window = _path_window(path, 5)
    if not window:
        return "走势概率  暂无足够历史样本", False
    win = _safe_num(window.get("win_probability")) * 100
    tail = _safe_num(window.get("q20_return_pct"))
    risk_level = str(risk.get("risk_level") or "")
    risk_label = _risk_level_label(risk_level)
    if tail < -6:
        tail_note = f"常见回撤可能到 {tail:.1f}%"
    elif tail < 0:
        tail_note = f"回撤压力约 {abs(tail):.1f}%"
    else:
        tail_note = "历史下沿仍为正"
    text = f"走势概率  5日上涨概率 {win:.0f}%  {tail_note}  风险 {risk_label}"
    return text, risk_level == "high"


def _calibration_plain_text(calibration: Dict[str, Any]) -> str:
    sample_size = int(_safe_num(calibration.get("sample_size")))
    hit_rate = calibration.get("hit_rate")
    if sample_size <= 0 or hit_rate is None:
        return "历史验证  相似样本不足，先按低置信度观察"
    avg = _safe_num(calibration.get("avg_forward_return_pct"))
    confidence = _safe_num(calibration.get("confidence_multiplier"), 1.0)
    return f"历史验证  相似样本 {sample_size} 次  5日成功率 {_safe_num(hit_rate) * 100:.0f}%  平均 {avg:.1f}%  置信 {confidence:.2f}"


def _snapshot_detail(row: Dict[str, Any]) -> str:
    title = f"{row.get('name', '-')} ({row.get('symbol', '-')})"
    return "\n".join([title, "=" * min(len(title), 24), "", *_beginner_summary_lines(row)])


def _committee_action_assessment(row: Dict[str, Any], result: Dict[str, Any]) -> str:
    op = row.get("operation") or {}
    snapshot_verdict = str(op.get("verdict") or "").upper()
    latest_verdict = str(result.get("verdict") or "").upper()
    snapshot_state = str(row.get("state") or "")
    snapshot_alloc = _safe_num(op.get("suggested_alloc_cny"))
    latest_alloc = _safe_num(result.get("suggested_alloc_cny"))
    if not result.get("success"):
        return "委员会分析失败，不能判断当前操作是否恰当。"
    if snapshot_state == "action_required" and latest_verdict in {"BUY", "ACCUMULATE"} and latest_alloc > 0:
        return "当前需要买入/加仓的操作与最新委员会方向一致，但仍需按止损和现金约束执行。"
    if snapshot_state == "action_required" and latest_verdict in {"TRIM", "SELL"} and latest_alloc < 0:
        return "当前卖出/减仓提醒与最新委员会方向一致，执行前按最新价格和可卖手数核对。"
    if snapshot_state == "action_required" and snapshot_alloc > 0 and latest_verdict in {"TRIM", "SELL"}:
        return "快照提示买入，但最新委员会偏向减仓/卖出，当前买入类动作不恰当。"
    if snapshot_verdict and latest_verdict and snapshot_verdict != latest_verdict:
        return f"快照是“{_verdict_label(snapshot_verdict)}”，最新分析是“{_verdict_label(latest_verdict)}”，需要以最新分析为准。"
    if latest_alloc < 0:
        return "最新委员会出现 verdict 与仓位方向不一致：金额为负数但文字结论偏观察。系统会按风险信号展示为待确认卖出，并建议重新跑一次分析。"
    if latest_alloc > 0:
        return "最新委员会虽然文字结论偏观察，但仓位建议为正数，应按候选买入处理，仍需核对买点和现金约束。"
    if latest_verdict in {"HOLD", "WAIT"} or latest_alloc == 0:
        return "最新委员会建议持仓观望，关注价格是否回到入场区间。"
    return "最新委员会仍给出方向性建议，操作前需要核对价格是否仍在买入/出场准则附近。"


def _format_committee_result(row: Dict[str, Any], result: Dict[str, Any]) -> str:
    title = f"{row.get('name', result.get('symbol', '-'))} ({row.get('symbol', result.get('symbol', '-'))})"
    lines = [title, "=" * min(len(title), 24), ""]
    if not result.get("success"):
        lines.extend(
            [
                "结论（最新）: 分析失败，先不要据此操作。",
                f"原因: {result.get('error') or '-'}",
                "",
                "下一步: 等下一轮监控，或稍后重新打开详情再跑一次。",
            ]
        )
        return "\n".join(lines)
    lines.extend(_beginner_summary_lines(row, result=result))
    lines.extend(["", "一致性检查:", _committee_action_assessment(row, result)])
    return "\n".join(lines)




__all__ = [
    "_label_state",
    "_state_signal",
    "_verdict_signal",
    "_row_tag",
    "_state_color",
    "_card_bg",
    "_stack_layer_bg",
    "_change_color",
    "_buy_summary",
    "_exit_summary",
    "_sector_summary",
    "_operation_summary",
    "_llm_review_lots_hint",
    "_verdict_label",
    "_compact_verdict_label",
    "_regime_label",
    "_extract_one_line",
    "_extract_risk_flags",
    "_distance_pct",
    "_fmt_distance",
    "_entry_exit_from_row",
    "_review_text_from_row",
    "_detail_line_style",
    "_beginner_summary_lines",
    "_beginner_next_step",
    "_path_window",
    "_risk_level_label",
    "_path_plain_text",
    "_calibration_plain_text",
    "_snapshot_detail",
    "_committee_action_assessment",
    "_format_committee_result",
]
