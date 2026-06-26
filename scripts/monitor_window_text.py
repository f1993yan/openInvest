"""Formatting and beginner-friendly explanation helpers for the desktop monitor window."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

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
    change = _safe_num((row.get("price") or {}).get("change_pct"))
    if state == "action_required":
        return "action"
    if state in {"trigger_confirmed", "watch_trigger"}:
        return "trigger"
    if state == "error":
        return "error"
    if state == "blocked":
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


def _operation_summary(row: Dict[str, Any]) -> str:
    op = row.get("operation") or {}
    status = str(op.get("status") or row.get("state") or "")
    lots = _suggested_lots(row)
    verdict = str(op.get("verdict") or "").upper()
    side = "卖" if verdict in {"SELL", "TRIM"} or lots < 0 else "买"
    if status == "action_required":
        return f"{side}{_fmt_lots(abs(lots))}" if lots else "操作"
    if status in {"trigger_confirmed", "watch_trigger"}:
        return "触发"
    if status == "candidate":
        if verdict in {"SELL", "TRIM"}:
            return "候选卖"
        if verdict in {"BUY", "ACCUMULATE"}:
            return "候选买"
        return "候选"
    if status == "blocked":
        return "拦截"
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
    ee = (source or {}).get("entry_exit_points") or _entry_exit_from_row(row)
    right_gate = (source or {}).get("right_side_trend_gate") or row.get("right_side_trend_gate") or {}
    review = str((source or {}).get("optimizer_review") or (source or {}).get("cio_memo") or _review_text_from_row(row))
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
    low_confidence = bool(
        (row.get("entry_exit_points") or {}).get("low_confidence")
        or technical.get("low_confidence")
    )
    if source:
        header = "结论（最新）"
        status_note = "已用刚刚跑完的委员会结果更新。"
    else:
        header = "结论（快照）"
        status_note = "下面是上次监控快照；最新分析完成后会自动刷新。"
    action = _verdict_label(verdict)
    if str(verdict or "").upper() in {"BUY", "ACCUMULATE"} and current > 0 and _safe_num(pullback) > 0 and current > _safe_num(pullback) * 1.03:
        decision = f"{action}，但当前价离回调买点偏高，别急着追。"
    elif str(verdict or "").upper() in {"BUY", "ACCUMULATE"}:
        decision = f"{action}，先看是否接近买点。"
    elif str(verdict or "").upper() in {"TRIM", "SELL"}:
        decision = f"{action}，重点看是否触发止损/减仓线。"
    else:
        decision = f"{action}，暂时以观察为主。"
    lines = [
        f"{header}: {decision}",
        status_note,
        "",
        "你最该先看这几项:",
        f"1. 当前价: {_fmt_price(current)}，今日涨跌 {_fmt_pct(price.get('change_pct'))}。",
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
    if one_line and one_line != "-":
        lines.append(f"- 模型提醒: {one_line}")
    if risk_flags or low_confidence:
        lines.extend(["", "需要小心:"])
        if low_confidence:
            lines.append("- 买卖点模型置信度偏低，价格线只能当参考，不能机械下单。")
        if plan_type == "a_share_position_exit":
            lines.append("- 已持仓标的的止盈止损盘中不重算，只在收盘后按追踪规则上移风险线。")
        for flag in risk_flags:
            lines.append(f"- {flag}")
    lines.extend(
        [
            "",
            "下一步:",
            _beginner_next_step(verdict, current, pullback, breakout, stop),
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


def _beginner_next_step(verdict: Any, current: Any, pullback: Any, breakout: Any, stop: Any) -> str:
    verdict_key = str(verdict or "").upper()
    current_num = _safe_num(current)
    pullback_num = _safe_num(pullback)
    breakout_num = _safe_num(breakout)
    stop_num = _safe_num(stop)
    if verdict_key in {"BUY", "ACCUMULATE"}:
        if pullback_num > 0 and current_num > pullback_num * 1.03:
            return f"先等回调接近 {_fmt_price(pullback_num)}，或放量站上 {_fmt_price(breakout_num)} 后再看；当前不适合盲目追高。"
        return "如果价格仍在买点附近，才考虑小仓位；买入前先确认能接受止损线。"
    if verdict_key in {"TRIM", "SELL"}:
        return f"如果跌破 {_fmt_price(stop_num)} 或反弹无力，优先降低风险。"
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
    latest_alloc = _safe_num(result.get("suggested_alloc_cny"))
    if not result.get("success"):
        return "委员会分析失败，不能判断当前操作是否恰当。"
    if snapshot_state == "action_required" and latest_verdict in {"BUY", "ACCUMULATE"} and latest_alloc > 0:
        return "当前需要买入/加仓的操作与最新委员会方向一致，但仍需按止损和现金约束执行。"
    if snapshot_state == "action_required" and latest_verdict in {"TRIM", "SELL"}:
        return "快照提示需要操作，但最新委员会偏向减仓/卖出，当前买入类动作不恰当。"
    if snapshot_verdict and latest_verdict and snapshot_verdict != latest_verdict:
        return f"快照是“{_verdict_label(snapshot_verdict)}”，最新分析是“{_verdict_label(latest_verdict)}”，需要以最新分析为准。"
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
