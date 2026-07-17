"""Right-side trend gate for A-share entry decisions.

This module answers one narrow question: after trend confirmation, is a
right-side BUY/ACCUMULATE allowed? It does not calculate stop-loss/take-profit
for existing positions and it does not replace ``core.entry_exit_points``.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RightSideTrendGate:
    allow: bool
    mode: str
    expected_return_after_confirmation_pct: float
    downside_risk_pct: float
    required_edge_pct: float
    trend_score: float
    committee_trend: str
    reason: str
    inputs: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def audit_text(self) -> str:
        return (
            "\n\n[RIGHT_SIDE_TREND_GATE]\n"
            "scope=right_side_entry_permission_only\n"
            "boundary=This gate may block BUY/ACCUMULATE, but it must not change "
            "ENTRY_EXIT_POINTS or POSITION_EXIT_POLICY.\n"
            f"allow={self.allow} mode={self.mode} committee_trend={self.committee_trend} "
            f"trend_score={self.trend_score:.2f}\n"
            f"expected_return_after_confirmation={self.expected_return_after_confirmation_pct:+.2f}% "
            f"downside_risk={self.downside_risk_pct:.2f}% required_edge={self.required_edge_pct:.2f}%\n"
            f"reason={self.reason}"
        )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        return default if math.isnan(out) else out
    except (TypeError, ValueError):
        return default


def _regime_label(regime_brief: str) -> str:
    for line in (regime_brief or "").splitlines():
        if line.startswith("REGIME:"):
            return line.split(":", 1)[1].strip().lower()
    return "unknown"


def _committee_trend_text(*texts: str) -> str:
    text = "\n".join(t or "" for t in texts).lower()
    bullish_hits = sum(key in text for key in ("uptrend", "bullish", "多头", "趋势向上", "上行", "突破"))
    bearish_hits = sum(key in text for key in ("downtrend", "bearish", "空头", "趋势向下", "下行", "破位"))
    if bearish_hits > bullish_hits:
        return "bearish"
    if bullish_hits > bearish_hits:
        return "bullish"
    return "neutral"


def evaluate_right_side_trend_gate(
    *,
    metrics: Dict[str, Any],
    regime_brief: str,
    optimizer_expected_return_pct: float,
    entry_exit_points: Optional[Dict[str, Any]] = None,
    conditional_return_stats: Optional[Any] = None,
    quant_view: str = "",
    risk_view: str = "",
    cio_memo: str = "",
    market: str = "a",
    is_holding: bool = False,
) -> RightSideTrendGate:
    if market.lower() != "a":
        return RightSideTrendGate(
            allow=True,
            mode="not_a_share",
            expected_return_after_confirmation_pct=round(optimizer_expected_return_pct, 4),
            downside_risk_pct=0.0,
            required_edge_pct=0.0,
            trend_score=0.0,
            committee_trend="neutral",
            reason="非A股，不启用A股右侧趋势闸门",
            inputs={},
        )
    if is_holding:
        return RightSideTrendGate(
            allow=True,
            mode="existing_position",
            expected_return_after_confirmation_pct=round(optimizer_expected_return_pct, 4),
            downside_risk_pct=0.0,
            required_edge_pct=0.0,
            trend_score=0.0,
            committee_trend="neutral",
            reason="已有持仓，本闸门只约束新开仓；加仓/减仓由优化器处理",
            inputs={},
        )

    ma20 = _safe_float(metrics.get("ma20"))
    ma120 = _safe_float(metrics.get("ma120"))
    price = _safe_float(metrics.get("current_price"))
    ret30 = _safe_float(metrics.get("return_30d")) * 100.0
    rsi = _safe_float(metrics.get("rsi14"), 50.0)
    rvol = _safe_float(metrics.get("rvol"), 1.0)
    atr_pct = max(_safe_float(metrics.get("atr_pct"), 2.0), 0.5)
    q80 = _safe_float(getattr(conditional_return_stats, "q80_return_pct", 0.0)) if conditional_return_stats else 0.0
    q20 = _safe_float(getattr(conditional_return_stats, "q20_return_pct", 0.0)) if conditional_return_stats else 0.0
    cvar = _safe_float(getattr(conditional_return_stats, "cvar_95_loss_pct", 0.0)) if conditional_return_stats else 0.0
    entry_exit_points = entry_exit_points or {}
    breakout = _safe_float(entry_exit_points.get("buy_breakout_price"))

    ma_spread_pct = (ma20 / ma120 - 1.0) * 100.0 if ma20 > 0 and ma120 > 0 else 0.0
    price_above_ma20 = price > ma20 > 0
    breakout_near = breakout > 0 and price >= breakout * 0.985
    regime = _regime_label(regime_brief)
    committee_trend = _committee_trend_text(quant_view, risk_view, cio_memo)

    trend_score = 0.0
    trend_score += 1.0 if regime in {"uptrend", "recovery"} else -1.0 if regime in {"downtrend", "crash"} else 0.0
    trend_score += 1.0 if ma_spread_pct >= 0.8 else -1.0 if ma_spread_pct <= -0.8 else 0.0
    trend_score += 0.8 if price_above_ma20 else -0.5
    trend_score += 0.7 if ret30 > 0 else -0.7 if ret30 < -3.0 else 0.0
    trend_score += 0.5 if breakout_near else 0.0
    trend_score += 0.4 if rvol >= 1.1 else -0.2 if rvol < 0.75 else 0.0
    trend_score += 0.4 if 45.0 <= rsi <= 72.0 else -0.5 if rsi > 82.0 or rsi < 35.0 else 0.0
    trend_score += 0.6 if committee_trend == "bullish" else -1.0 if committee_trend == "bearish" else 0.0

    expected_after_confirmation = max(float(optimizer_expected_return_pct), q80 * 0.65 if q80 else float(optimizer_expected_return_pct))
    downside_risk = max(abs(q20), cvar, 1.2 * atr_pct, 1.0)
    required_edge = max(0.6, 0.35 * downside_risk)
    allow = (
        trend_score >= 2.2
        and expected_after_confirmation > required_edge
        and committee_trend != "bearish"
        and regime not in {"downtrend", "crash"}
    )
    if allow:
        reason = "趋势确认且确认后的条件期望收益覆盖下行风险"
    elif committee_trend == "bearish":
        reason = "委员会趋势判断偏空，禁止右侧追涨"
    elif regime in {"downtrend", "crash"}:
        reason = f"regime={regime}，右侧交易条件不成立"
    elif expected_after_confirmation <= required_edge:
        reason = "确认后的条件期望收益不足以覆盖下行风险"
    else:
        reason = "趋势确认分数不足"

    return RightSideTrendGate(
        allow=allow,
        mode="right_side_confirmation",
        expected_return_after_confirmation_pct=round(expected_after_confirmation, 4),
        downside_risk_pct=round(downside_risk, 4),
        required_edge_pct=round(required_edge, 4),
        trend_score=round(trend_score, 4),
        committee_trend=committee_trend,
        reason=reason,
        inputs={
            "regime": regime,
            "ma_spread_pct": round(ma_spread_pct, 4),
            "price_above_ma20": price_above_ma20,
            "return_30d_pct": round(ret30, 4),
            "rsi14": round(rsi, 4),
            "rvol": round(rvol, 4),
            "breakout_near": breakout_near,
        },
    )


__all__ = ["RightSideTrendGate", "evaluate_right_side_trend_gate"]
