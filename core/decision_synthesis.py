"""User-facing synthesis for committee, optimizer and LLM review outputs.

The deterministic optimizer remains the executable authority. LLM review is
treated as weak audit evidence: it can explain, caution, or flag a conflict, but
it should not create a second competing final decision in the UI.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


ACTION_LABELS = {
    "BUY": "买入",
    "ACCUMULATE": "小幅加仓",
    "HOLD": "观察/持有",
    "WAIT": "等待",
    "TRIM": "减仓",
    "SELL": "卖出",
}


@dataclass(frozen=True)
class DecisionSynthesis:
    final_action: str
    action_label: str
    decision_level: str
    primary_reason: str
    confidence: float
    evidence: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    risk_warnings: List[str] = field(default_factory=list)
    user_strategy: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def audit_text(self) -> str:
        return (
            "\n\n[DECISION_SYNTHESIS]\n"
            f"final_action={self.final_action} level={self.decision_level} confidence={self.confidence:.2f}\n"
            f"primary_reason={self.primary_reason}\n"
            f"evidence={' | '.join(self.evidence)}\n"
            f"risk_warnings={' | '.join(self.risk_warnings)}\n"
            f"conflicts={' | '.join(self.conflicts)}\n"
            f"user_strategy={self.user_strategy}"
        )


def synthesize_decision(
    *,
    symbol: str,
    name: str = "",
    optimizer: Any = None,
    parsed: Optional[Dict[str, Any]] = None,
    entry_exit_points: Optional[Dict[str, Any]] = None,
    right_side_gate: Optional[Dict[str, Any]] = None,
    position_exit_policy: Optional[Dict[str, Any]] = None,
    optimizer_review: str = "",
    current_price: Optional[float] = None,
    is_holding: bool = False,
    execution_blocked: bool = False,
    execution_block_reason: str = "",
) -> DecisionSynthesis:
    parsed = parsed or {}
    entry_exit_points = entry_exit_points or {}
    right_side_gate = right_side_gate or {}
    position_exit_policy = position_exit_policy or {}
    optimizer_action = str(getattr(optimizer, "verdict", "") or "").upper()
    parsed_action = str(parsed.get("verdict") or "").upper()
    final_action = parsed_action or optimizer_action or "HOLD"
    if execution_blocked:
        final_action = "HOLD"
    confidence = float(getattr(optimizer, "confidence", parsed.get("confidence", 0.0)) or 0.0)
    alloc = float(getattr(optimizer, "alloc_cny", parsed.get("alloc_cny", 0.0)) or 0.0)
    level = _decision_level(final_action, alloc, execution_blocked)

    evidence: List[str] = []
    conflicts: List[str] = []
    risk_warnings: List[str] = []
    llm_conclusion = _review_conclusion(optimizer_review)
    llm_one_line = _extract_prefixed(optimizer_review, "ONE_LINE")
    llm_flags = _extract_flags(optimizer_review)
    expected_return = getattr(optimizer, "expected_return_pct", None)
    p_directional = getattr(optimizer, "p_directional", None)
    rr = _safe_float(entry_exit_points.get("reward_risk_ratio"))
    if expected_return is not None:
        evidence.append(f"优化器30日预期收益 {float(expected_return):+.1f}%")
    if p_directional is not None:
        evidence.append(f"方向胜率估计 {float(p_directional) * 100:.0f}%")
    if rr > 0:
        evidence.append(f"当前买卖点盈亏比 {rr:.1f}")
    if right_side_gate:
        gate_text = "右侧趋势通过" if right_side_gate.get("allow") else "右侧趋势未通过"
        reason = str(right_side_gate.get("reason") or "")
        evidence.append(f"{gate_text}{f'({reason})' if reason else ''}")
    policy_score = _safe_float(position_exit_policy.get("policy_quality_score"), None)
    if policy_score is not None:
        evidence.append(f"止盈止损策略质量 {policy_score:+.1f}")
    if llm_one_line:
        evidence.append(f"LLM审核: {llm_one_line}")

    llm_conflicts = {
        ("reject", "BUY"),
        ("reject", "ACCUMULATE"),
        ("caution", "BUY"),
        ("caution", "ACCUMULATE"),
        ("approve", "SELL"),
        ("approve", "TRIM"),
    }
    if (llm_conclusion, final_action) in llm_conflicts:
        conflicts.append(f"LLM审核为{llm_conclusion}，但确定性优化器给出{ACTION_LABELS.get(final_action, final_action)}")
    if optimizer_action and optimizer_action != final_action:
        conflicts.append(f"优化器原始动作{ACTION_LABELS.get(optimizer_action, optimizer_action)}，执行保护后最终为{ACTION_LABELS.get(final_action, final_action)}")
    if execution_blocked:
        risk_warnings.append(f"执行保护已拦截: {execution_block_reason or '交易约束'}")
    risk_warnings.extend(llm_flags[:3])
    if not right_side_gate.get("allow") and final_action in {"BUY", "ACCUMULATE"}:
        risk_warnings.append("右侧趋势尚未确认，买入必须等待触发价")
    if is_holding and final_action in {"TRIM", "SELL"} and position_exit_policy:
        risk_warnings.append("卖出以持仓纪律线为准，盘中不随意重算止盈止损")

    primary_reason = _primary_reason(final_action, level, evidence, risk_warnings)
    strategy = _strategy_text(
        final_action=final_action,
        current_price=current_price,
        entry_exit_points=entry_exit_points,
        is_holding=is_holding,
        execution_blocked=execution_blocked,
    )
    return DecisionSynthesis(
        final_action=final_action,
        action_label=ACTION_LABELS.get(final_action, final_action),
        decision_level=level,
        primary_reason=primary_reason,
        confidence=round(max(0.0, min(1.0, confidence)), 4),
        evidence=evidence[:5],
        conflicts=conflicts[:3],
        risk_warnings=_dedupe(risk_warnings)[:4],
        user_strategy=strategy,
    )


def _decision_level(final_action: str, alloc: float, execution_blocked: bool) -> str:
    if execution_blocked:
        return "blocked"
    if final_action in {"BUY", "ACCUMULATE", "TRIM", "SELL"} and abs(alloc) > 0:
        return "action_candidate"
    if final_action in {"BUY", "ACCUMULATE", "TRIM", "SELL"}:
        return "watch_trigger"
    return "watch"


def _primary_reason(action: str, level: str, evidence: List[str], warnings: List[str]) -> str:
    if level == "blocked":
        return warnings[0] if warnings else "交易约束已拦截，当前不执行"
    if evidence:
        return evidence[0]
    if action in {"BUY", "ACCUMULATE"}:
        return "有买入倾向，但需要价格触发确认"
    if action in {"TRIM", "SELL"}:
        return "有卖出/降风险倾向，关注纪律线是否触发"
    return "暂无明确优势，继续观察"


def _strategy_text(
    *,
    final_action: str,
    current_price: Optional[float],
    entry_exit_points: Dict[str, Any],
    is_holding: bool,
    execution_blocked: bool,
) -> str:
    if execution_blocked:
        return "本轮不执行，等下一轮监控或人工确认。"
    price = _safe_float(current_price)
    pullback = _safe_float(entry_exit_points.get("buy_pullback_price"))
    breakout = _safe_float(entry_exit_points.get("buy_breakout_price"))
    stop = _safe_float(entry_exit_points.get("stop_loss_price"))
    take = _safe_float(entry_exit_points.get("take_profit_price"))
    if final_action in {"BUY", "ACCUMULATE"}:
        if price and pullback and price > pullback * 1.03:
            return f"不追高，优先等回踩接近{pullback:.2f}；若放量站上{breakout:.2f}再按小仓试错。"
        return f"只有价格仍在买点附近才执行，买入后用{stop:.2f}作为风险线。"
    if final_action in {"TRIM", "SELL"}:
        if is_holding:
            return f"持仓优先看{stop:.2f}风险线和{take:.2f}止盈线，触发后按手数执行。"
        return "当前无持仓，卖出结论只作为风险提示。"
    return "先观察，不主动交易；等待价格接近买点、止损点或趋势闸门重新确认。"


def _review_conclusion(text: str) -> str:
    value = _extract_prefixed(text, "CONCLUSION").lower()
    if value.startswith("reject"):
        return "reject"
    if value.startswith("caution"):
        return "caution"
    if value.startswith("approve") or value.startswith("keep"):
        return "approve"
    return ""


def _extract_prefixed(text: str, key: str) -> str:
    match = re.search(rf"{re.escape(key)}:\s*(.+?)(?=\s+[A-Z_]+:|$)", text or "", re.S)
    return match.group(1).strip() if match else ""


def _extract_flags(text: str) -> List[str]:
    raw = _extract_prefixed(text, "RISK_FLAGS")
    if not raw:
        return []
    return [item.strip(" ，,;；") for item in re.split(r"[,，;；]", raw) if item.strip(" ，,;；")]


def _safe_float(value: Any, default: Any = 0.0) -> Any:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _dedupe(items: List[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        item = str(item or "").strip()
        if item and item not in out:
            out.append(item)
    return out


__all__ = ["DecisionSynthesis", "synthesize_decision"]
