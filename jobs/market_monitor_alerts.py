"""Cash/risk aware alert selection for market monitor."""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from jobs.market_monitor_common import _clamp, _safe_num
from jobs.market_monitor_entry_exit import (
    evaluate_entry_exit_triggers,
    _stock_units,
)
from jobs.market_monitor_guards import _is_limit_up_buy_blocked
from jobs.market_monitor_guards import _trigger_has_price_progress_from_trade
from jobs.trading_mode import (
    DEFAULT_TRADING_MODE,
    normalize_trading_mode,
    trading_mode_buy_lot_multiplier,
    trading_mode_buy_threshold,
    trading_mode_buy_value_adjustment,
    trading_mode_cash_reserve_ratio,
    trading_mode_label,
    trading_mode_sell_score_bonus,
    trading_mode_sell_threshold_adjustment,
)

SELL_ALERT_BASE_THRESHOLD = 45.0
SELL_ALERT_TRIGGER_FLOOR = 38.0
SELL_ALERT_COMMITTEE_FLOOR = 34.0
BUY_ALERT_THRESHOLD = 55.0


def _has_llm_hold_conflict(result: Dict[str, Any]) -> bool:
    memo = str(result.get("cio_memo", ""))
    return "LLM=HOLD" in memo and ("-> ACCUMULATE" in memo or "-> BUY" in memo)


_REVIEW_APPROVE_TOKENS = {"APPROVE", "APPROVED", "SUPPORT", "SUPPORTED", "KEEP", "PASS", "OK", "ACCEPT"}
_REVIEW_CAUTION_TOKENS = {"CAUTION", "CAUTIOUS", "WARN", "WARNING", "WATCH", "REVIEW"}
_REVIEW_REJECT_TOKENS = {"REJECT", "REJECTED", "BLOCK", "VETO", "DENY", "AVOID"}

# LLM review is qualitative evidence, not the primary optimizer.  Treat it as a
# weak likelihood-ratio update on the objective score's implied odds:
#   score = threshold + scale * log(odds)
#   posterior_score = score + scale * log(likelihood_ratio)
# These values are conservative priors, not fitted optima.  They can be
# replaced by historical calibration once executed/ignored alert outcomes exist.
_LLM_REVIEW_LOGIT_SCALE = 12.0
_REVIEW_LIKELIHOOD_RATIOS = {
    "approve": 1.12,
    "caution": 0.78,
    "reject": 0.48,
}
_LLM_HOLD_CONFLICT_LR_WITH_TRIGGER = 0.86
_LLM_HOLD_CONFLICT_LR_WITHOUT_TRIGGER = 0.70


def _map_review_conclusion(token: str) -> str:
    normalized = str(token or "").strip().upper().replace("-", "_")
    if not normalized:
        return ""
    if normalized in _REVIEW_REJECT_TOKENS or any(
        phrase in token for phrase in ("拒绝", "否决", "反对", "不通过", "不建议", "放弃")
    ):
        return "reject"
    if normalized in _REVIEW_CAUTION_TOKENS or any(
        phrase in token for phrase in ("谨慎", "慎重", "观察", "观望", "风险偏高")
    ):
        return "caution"
    if normalized in _REVIEW_APPROVE_TOKENS or any(
        phrase in token for phrase in ("通过", "支持", "认可", "同意", "可以")
    ):
        return "approve"
    return ""


def _review_conclusion(result: Dict[str, Any]) -> str:
    text = f"{result.get('optimizer_review', '')}\n{result.get('cio_memo', '')}"
    match = re.search(r"(?i)\bCONCLUSION\s*[:：]\s*([A-Z_]+|[\u4e00-\u9fff]+)", text)
    if match:
        return _map_review_conclusion(match.group(1))
    for phrase in ("明确反对", "不建议执行", "建议放弃", "暂不买入", "风险偏高"):
        if phrase in text:
            return "reject" if phrase != "风险偏高" else "caution"
    return ""


def _review_score_adjustment(
    result: Dict[str, Any],
    objective_score: float,
    *,
    threshold: float = 55.0,
) -> float:
    _ = (objective_score, threshold)
    conclusion = _review_conclusion(result)
    return _likelihood_ratio_score_adjustment(
        _REVIEW_LIKELIHOOD_RATIOS.get(conclusion, 1.0),
        scale=_LLM_REVIEW_LOGIT_SCALE,
    )


def _likelihood_ratio_score_adjustment(
    likelihood_ratio: float,
    *,
    scale: float = _LLM_REVIEW_LOGIT_SCALE,
) -> float:
    lr = _safe_num(likelihood_ratio, 1.0)
    if lr <= 0 or not math.isfinite(lr):
        return 0.0
    return scale * math.log(lr)


def _llm_hold_conflict_adjustment(
    result: Dict[str, Any],
    objective_score: float,
    *,
    has_buy_trigger: bool,
    threshold: float = 55.0,
) -> float:
    _ = (objective_score, threshold)
    if not _has_llm_hold_conflict(result):
        return 0.0
    lr = _LLM_HOLD_CONFLICT_LR_WITH_TRIGGER if has_buy_trigger else _LLM_HOLD_CONFLICT_LR_WITHOUT_TRIGGER
    return _likelihood_ratio_score_adjustment(lr, scale=_LLM_REVIEW_LOGIT_SCALE)


def _sell_position_pressure(result: Dict[str, Any], stock: Dict[str, Any], price: float) -> float:
    """How much existing holding risk the committee wants to remove.

    For a held stock, selling is a risk-release decision.  A TRIM/SELL verdict
    with a large negative allocation should require less extra price-trigger
    evidence than a tiny trim, because the optimizer is already saying the
    current position is too risky.  The output is bounded in [0, 1].
    """
    alloc = abs(_safe_num(result.get("suggested_alloc_cny")))
    if alloc <= 0:
        return 0.0
    units = _safe_num(stock.get("units"))
    cost = _safe_num(stock.get("cost"))
    holding_value = 0.0
    if units > 0 and price > 0:
        holding_value = units * price
    elif units > 0 and cost > 0:
        holding_value = units * cost
    position_pct = _safe_num(stock.get("position_pct"))
    if holding_value <= 0 and position_pct <= 0:
        return 0.0
    if holding_value > 0:
        return _clamp(alloc / holding_value, 0.0, 1.0)
    return _clamp(position_pct / 20.0, 0.0, 1.0)


def _sell_committee_execution_edge(
    result: Dict[str, Any],
    stock: Dict[str, Any],
    price_info: Dict[str, Any],
) -> float:
    """Bounded evidence that a held sell should become executable now.

    This combines independent pieces of evidence into a conservative expected
    utility proxy:

    - committee confidence and SELL/TRIM direction;
    - negative allocation size relative to the held position;
    - adverse same-day price movement for existing holdings.

    It is intentionally bounded so it can lower the alert threshold, but cannot
    by itself make every weak TRIM noisy.
    """
    verdict = str(result.get("verdict", "")).upper()
    if verdict not in {"TRIM", "SELL"}:
        return 0.0
    if _safe_num(stock.get("position_pct")) <= 0 and _safe_num(stock.get("units")) <= 0:
        return 0.0
    confidence = _clamp(_safe_num(result.get("confidence")), 0.0, 1.0)
    direction_edge = 1.0 if verdict == "SELL" else 0.72
    pressure = _sell_position_pressure(result, stock, _safe_num(price_info.get("price")))
    change_pct = _safe_num(price_info.get("change_pct"))
    price_edge = _clamp((-change_pct) / 5.0, -0.4, 0.8)
    raw = (
        0.48 * (confidence * direction_edge)
        + 0.36 * pressure
        + 0.16 * max(0.0, price_edge)
    )
    return _clamp(raw, 0.0, 1.0)


def _sell_alert_threshold(result: Dict[str, Any], triggers: List[Dict[str, Any]]) -> float:
    """Minimum score for held TRIM/SELL alerts.

    Production sell alerts are committee/optimizer driven.  Historical
    cost-anchored position discipline does not lower or raise this threshold.
    """
    has_sell_trigger = any(t.get("side") == "sell" for t in triggers)
    threshold = SELL_ALERT_BASE_THRESHOLD
    if has_sell_trigger:
        threshold = SELL_ALERT_TRIGGER_FLOOR

    floor = SELL_ALERT_TRIGGER_FLOOR if has_sell_trigger else SELL_ALERT_BASE_THRESHOLD
    return round(_clamp(threshold, floor, 52.0), 2)


def _sell_committee_alert_threshold(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    price_info: Dict[str, Any],
    triggers: List[Dict[str, Any]],
    trading_mode: str = DEFAULT_TRADING_MODE,
) -> float:
    """Execution threshold for held TRIM/SELL decisions.

    Price triggers still get the strict policy threshold.  Without a trigger,
    high-confidence SELL/TRIM decisions on existing positions can become
    actionable when their conservative execution edge is strong enough.  This
    removes the old buy/sell asymmetry: buys were allowed through on committee
    score alone, while sells were effectively forced to wait for a line touch.
    """
    mode = normalize_trading_mode(trading_mode)
    threshold = _sell_alert_threshold(result, triggers)
    threshold += trading_mode_sell_threshold_adjustment(
        mode,
        has_trigger=any(t.get("side") == "sell" for t in triggers),
        policy_edge=0.0,
    )
    mode_floor_offset = 4.0 if mode != DEFAULT_TRADING_MODE else 0.0
    if any(t.get("side") == "sell" for t in triggers):
        return round(_clamp(threshold, SELL_ALERT_TRIGGER_FLOOR - mode_floor_offset, 52.0), 2)
    edge = _sell_committee_execution_edge(result, stock, price_info)
    if edge <= 0:
        return threshold
    verdict = str(result.get("verdict", "")).upper()
    floor = SELL_ALERT_COMMITTEE_FLOOR if verdict == "SELL" else SELL_ALERT_COMMITTEE_FLOOR + 2.0
    # Edge is in [0, 1].  Strong evidence can reduce the no-trigger threshold
    # by up to 9 points, but the floor keeps marginal sells as candidates.
    threshold -= 9.0 * edge
    return round(_clamp(threshold, floor - mode_floor_offset, 52.0), 2)


def _sell_candidate_wait_reason(
    *,
    result: Dict[str, Any],
    score: float,
    threshold: float,
    triggers: List[Dict[str, Any]],
) -> str:
    verdict = str(result.get("verdict", "")).upper()
    if not any(t.get("side") == "sell" for t in triggers):
        return f"sell_waiting_for_trigger_or_edge:{score:.1f}<{threshold:.1f}:{verdict}"
    return f"low_sell_score:{score:.1f}<{threshold:.1f}:{verdict}"


def _is_existing_position(stock: Dict[str, Any]) -> bool:
    return _safe_num(stock.get("position_pct")) > 0 or _stock_units(stock) > 0


def _is_a_share_position(result: Dict[str, Any], stock: Dict[str, Any]) -> bool:
    market = str(result.get("market") or stock.get("market") or "").strip().lower()
    symbol = str(result.get("symbol") or stock.get("symbol") or "").strip().upper()
    if market in {"a", "ashare", "a-share", "cn", "china", "sse", "szse", "sh", "sz"}:
        return True
    return symbol.isdigit() and len(symbol) == 6


def _same_day_buy_units(
    symbol: str,
    same_day_bought_units_by_symbol: Optional[Dict[str, float]],
) -> float:
    if not same_day_bought_units_by_symbol:
        return 0.0
    return _safe_num(same_day_bought_units_by_symbol.get(symbol.upper()))


def _alert_lot_size(stock: Dict[str, Any], result: Dict[str, Any]) -> int:
    return max(1, int(stock.get("min_lot_size") or result.get("min_lot_size") or 100))


def _sellable_units_for_alert(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    same_day_bought_units_by_symbol: Optional[Dict[str, float]],
) -> float:
    units = _stock_units(stock)
    symbol = str(result.get("symbol") or stock.get("symbol") or "").upper()
    if _is_a_share_position(result, stock):
        units -= _same_day_buy_units(symbol, same_day_bought_units_by_symbol)
    return max(0.0, units)


def _sellable_lots_for_alert(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    same_day_bought_units_by_symbol: Optional[Dict[str, float]],
) -> int:
    lot = _alert_lot_size(stock, result)
    sellable_units = _sellable_units_for_alert(
        result,
        stock=stock,
        same_day_bought_units_by_symbol=same_day_bought_units_by_symbol,
    )
    if sellable_units <= 0:
        return 0
    return int(math.floor(sellable_units / lot))


def _same_day_t1_block_reason(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    same_day_bought_units_by_symbol: Optional[Dict[str, float]],
) -> str:
    symbol = str(result.get("symbol") or stock.get("symbol") or "").upper()
    bought_units = _same_day_buy_units(symbol, same_day_bought_units_by_symbol)
    if bought_units > 0 and _is_a_share_position(result, stock):
        return "same_day_a_share_t1_sell_blocked"
    return "no_sellable_lots"


def _recent_trade_for_symbol(
    symbol: str,
    recent_trades_by_symbol: Optional[Dict[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    if not recent_trades_by_symbol:
        return {}
    return dict(recent_trades_by_symbol.get(symbol.upper()) or {})


def _trade_direction(trade: Dict[str, Any]) -> str:
    return str(trade.get("direction") or "").upper()


def _direction_has_trigger(direction: str, triggers: List[Dict[str, Any]]) -> bool:
    side = direction.lower()
    return any(str(t.get("side") or "").lower() == side for t in triggers)


def _opposite_trade_wait_reason(
    *,
    direction: str,
    triggers: List[Dict[str, Any]],
    current_price: float,
    recent_trade: Dict[str, Any],
) -> str:
    if not recent_trade:
        return ""
    if _trade_direction(recent_trade) == direction:
        return ""
    # Position-exit discipline is retired. A committee/optimizer SELL is
    # constrained by sellable units and A-share T+1, not an obsolete price line.
    if direction == "SELL":
        return ""
    if not _direction_has_trigger(direction, triggers):
        return f"recent_opposite_real_trade_without_new_entry_exit_trigger:{direction}"
    if not _trigger_has_price_progress_from_trade(
        direction=direction,
        triggers=triggers,
        current_price=current_price,
        previous_trade=recent_trade,
    ):
        return f"recent_opposite_real_trade_without_price_progress:{direction}"
    return ""


def _sell_candidate_wait_label(reason: str) -> str:
    text = str(reason or "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)<([0-9]+(?:\.[0-9]+)?)", text)
    if "discipline_review_wait" in text or "sector_panic_guard" in text:
        return "旧持仓纪律复核已停用，等待委员会/优化器重新给出方向"
    if "same_day_a_share_t1_sell_blocked" in text:
        base = "A股当天买入部分不可当天卖出"
    elif "waiting_for_current_exit_trigger" in text:
        base = "未触发当前止损/止盈线，暂不提示卖出"
    elif "waiting_for_trigger_or_edge" in text:
        base = "卖出证据不足，未触发纪律线"
    elif "low_sell_score" in text:
        base = "卖出评分不足"
    else:
        base = "卖出条件未满足"
    if match:
        return f"{base}（{match.group(1)}/{match.group(2)}）"
    return base


def _suppressed_reasons_by_symbol(suppressed_alerts: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for item in suppressed_alerts:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        reason = str(item.get("reason") or "suppressed")
        out.setdefault(symbol, []).append(_sell_candidate_wait_label(reason))
    return out


def _entry_trigger_for_result(
    result: Dict[str, Any],
    *,
    current_price: float,
    stock: Dict[str, Any],
    entry_exit_state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    symbol = str(result.get("symbol") or stock.get("symbol") or "").upper()
    previous = ((entry_exit_state.get("symbols") or {}).get(symbol) or {})
    is_holding = _safe_num(stock.get("position_pct")) > 0 or _stock_units(stock) > 0
    if is_holding:
        return []
    return evaluate_entry_exit_triggers(
        current_price,
        previous.get("entry_exit_points"),
        is_holding=False,
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
    has_buy_trigger = any(t.get("side") == "buy" for t in triggers)
    if has_buy_trigger:
        score += 24.0
    elif verdict in {"BUY", "ACCUMULATE"}:
        score -= 18.0
    has_sell_trigger = any(t.get("side") == "sell" for t in triggers)
    if has_sell_trigger and verdict in {"TRIM", "SELL"}:
        score += 20.0
    elif has_sell_trigger and verdict in {"BUY", "ACCUMULATE"}:
        score -= 8.0

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

    objective_score = score
    score += _review_score_adjustment(result, objective_score)
    score += _llm_hold_conflict_adjustment(result, objective_score, has_buy_trigger=has_buy_trigger)
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


def _text_percent_after_keywords(text: str, keywords: Tuple[str, ...]) -> float:
    if not text:
        return 0.0
    escaped = "|".join(re.escape(k) for k in keywords)
    match = re.search(rf"(?i)(?:{escaped})[^\d%负亏损-]*(-?\d+(?:\.\d+)?)\s*%", text)
    if not match:
        return 0.0
    return abs(_safe_num(match.group(1)))


def _tail_loss_pct(result: Dict[str, Any]) -> float:
    ee = result.get("entry_exit_points") or {}
    direct = max(
        _safe_num(result.get("cvar_95_loss_pct")),
        _safe_num(result.get("conditional_cvar_95_loss_pct")),
        _safe_num(ee.get("cvar_95_loss_pct")),
        _safe_num(ee.get("conditional_cvar_95_loss_pct")),
    )
    if direct > 0:
        return direct
    text = f"{result.get('optimizer_review', '')}\n{result.get('cio_memo', '')}"
    return _text_percent_after_keywords(text, ("CVaR", "cvar", "条件CVaR", "尾部风险", "尾部损失"))


def _position_scale_label(scale: float) -> str:
    if scale <= 0.15:
        return "avoid"
    if scale <= 0.35:
        return "tiny"
    if scale <= 0.70:
        return "half"
    return "normal"


def _position_scale_multiplier(label: str) -> float:
    return {
        "avoid": 0.0,
        "tiny": 0.25,
        "half": 0.5,
        "normal": 1.0,
    }.get(label, 1.0)


def _math_review_position_scale(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    price: float,
    lots: int,
    lot_size: int,
    portfolio_value: float,
    alert_score: float,
    max_single_position_pct: float,
) -> Dict[str, Any]:
    if lots <= 0 or price <= 0 or lot_size <= 0:
        return {
            "position_scale": "avoid",
            "position_scale_value": 0.0,
            "position_scale_multiplier": 0.0,
            "recommended_lots": 0,
            "risk_components": {},
        }

    ee = result.get("entry_exit_points") or {}
    cost = lots * lot_size * price
    add_pct = cost / max(portfolio_value, 1.0) * 100.0
    current_pct = _safe_num(stock.get("position_pct"))
    target_pct = _safe_num(stock.get("target_position_pct") or stock.get("target_pct"), max_single_position_pct)
    target_pct = min(max_single_position_pct, target_pct if target_pct > 0 else max_single_position_pct)

    stop_loss = _safe_num(ee.get("stop_loss_price"))
    if stop_loss > 0 and stop_loss < price:
        stop_loss_pct = (price - stop_loss) / price * 100.0
    else:
        stop_loss_pct = max(_safe_num(ee.get("atr_pct")), 2.0)
    stop_loss_budget_pct = 1.0
    stop_loss_loss_pct = add_pct * stop_loss_pct / 100.0
    stop_loss_scale = 1.0 if stop_loss_loss_pct <= 0 else _clamp(stop_loss_budget_pct / stop_loss_loss_pct, 0.0, 1.0)

    cvar_pct = _tail_loss_pct(result)
    tail_budget_pct = 1.5
    tail_loss_pct = add_pct * cvar_pct / 100.0
    tail_scale = 1.0 if tail_loss_pct <= 0 else _clamp(tail_budget_pct / tail_loss_pct, 0.0, 1.0)

    remaining_position_pct = max(0.0, target_pct - current_pct)
    concentration_scale = 1.0 if add_pct <= 0 else _clamp(remaining_position_pct / add_pct, 0.0, 1.0)

    signal_scale = _clamp(1.0 / (1.0 + math.exp(-(_safe_num(alert_score) - 65.0) / 8.0)), 0.0, 1.0)

    raw_scale = min(stop_loss_scale, tail_scale, concentration_scale, signal_scale)
    label = _position_scale_label(raw_scale)
    multiplier = _position_scale_multiplier(label)
    recommended_lots = int(math.floor(lots * multiplier))
    if label != "avoid" and lots > 0:
        recommended_lots = max(1, recommended_lots)

    return {
        "position_scale": label,
        "position_scale_value": round(raw_scale, 4),
        "position_scale_multiplier": multiplier,
        "recommended_lots": min(lots, recommended_lots),
        "risk_components": {
            "stop_loss_scale": round(stop_loss_scale, 4),
            "stop_loss_loss_pct": round(stop_loss_loss_pct, 4),
            "cvar_scale": round(tail_scale, 4),
            "cvar_95_loss_pct": round(cvar_pct, 4),
            "tail_loss_pct": round(tail_loss_pct, 4),
            "concentration_scale": round(concentration_scale, 4),
            "signal_scale": round(signal_scale, 4),
            "add_position_pct": round(add_pct, 4),
            "post_position_pct": round(current_pct + add_pct, 4),
        },
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
    trading_mode: str = DEFAULT_TRADING_MODE,
    same_day_bought_units_by_symbol: Optional[Dict[str, float]] = None,
    recent_real_trades_by_symbol: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Pick executable alerts under cash and risk-distribution budgets.

    This is a multiple-choice knapsack over board-lot buy options, with sells
    admitted separately because they release risk/cash instead of consuming it.
    Buy candidates are scored by signal quality minus stop-loss, position, and
    sector-concentration penalties, so the selected basket is diversified rather
    than merely the highest raw score that fits the cash budget.
    """
    stock_by_symbol = {str(s.get("symbol", "")).upper(): s for s in stocks}
    mode = normalize_trading_mode(trading_mode)
    mode_label = trading_mode_label(mode)
    existing_sector_pct = _existing_sector_exposure(stocks)
    # If the caller does not provide total assets, treat available cash as
    # roughly a 20% cash sleeve. This keeps legacy tests/callers from being
    # over-penalized as if every buy consumed nearly the whole portfolio.
    inferred_portfolio_value = max(_safe_num(cash) * 5.0, _safe_num(cash), 1.0)
    base_portfolio_value = max(_safe_num(portfolio_value), inferred_portfolio_value)
    reserve_cash = base_portfolio_value * trading_mode_cash_reserve_ratio(mode)
    buy_cash_budget = max(0.0, _safe_num(cash) - reserve_cash)
    buy_threshold = trading_mode_buy_threshold(mode)
    buy_value_adjustment = trading_mode_buy_value_adjustment(mode)
    buy_lot_multiplier = trading_mode_buy_lot_multiplier(mode)
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

        if verdict in {"HOLD", "REDUCE", "UNCLEAR"} or abs(alloc) <= 0:
            continue

        score = _action_score(result, stock=stock, price_info=price_info, triggers=triggers)

        if verdict in {"TRIM", "SELL"} and alloc < 0:
            recent_trade = _recent_trade_for_symbol(symbol, recent_real_trades_by_symbol)
            wait_reason = _opposite_trade_wait_reason(
                direction="SELL",
                triggers=triggers,
                current_price=price,
                recent_trade=recent_trade,
            )
            if wait_reason:
                suppressed.append({
                    "symbol": symbol,
                    "name": result.get("name"),
                    "reason": wait_reason,
                    "recent_trade": recent_trade,
                    "alert_source": "trade_cooldown",
                })
                continue
            sellable_lots = _sellable_lots_for_alert(
                result,
                stock=stock,
                same_day_bought_units_by_symbol=same_day_bought_units_by_symbol,
            )
            if sellable_lots <= 0:
                suppressed.append({
                    "symbol": symbol,
                    "name": result.get("name"),
                    "reason": _same_day_t1_block_reason(
                        result,
                        stock=stock,
                        same_day_bought_units_by_symbol=same_day_bought_units_by_symbol,
                    ),
                    "discipline_review": {},
                    "alert_source": "committee_sell",
                })
                continue
            threshold = _sell_committee_alert_threshold(
                result,
                stock=stock,
                price_info=price_info,
                triggers=triggers,
                trading_mode=mode,
            )
            release_cash_cny = abs(alloc)
            score += trading_mode_sell_score_bonus(
                mode,
                release_cash_cny=release_cash_cny,
                portfolio_value=base_portfolio_value,
            )
            if score >= threshold:
                selected = dict(result)
                selected["alert_score"] = round(score, 2)
                selected["alert_threshold"] = threshold
                selected["alert_triggers"] = triggers
                selected["alert_source"] = "committee_sell"
                selected["discipline_review"] = {}
                selected["optimizer_lots"] = min(
                    int(abs(alloc) // max(price * _alert_lot_size(stock, result), 1.0)),
                    sellable_lots,
                )
                if selected["optimizer_lots"] <= 0:
                    selected["optimizer_lots"] = min(1, sellable_lots)
                selected["alert_selected_lots"] = selected["optimizer_lots"]
                selected["llm_review_lots"] = selected["optimizer_lots"]
                selected["trading_mode"] = mode
                selected["trading_mode_label"] = mode_label
                sell_alerts.append(selected)
            else:
                suppressed.append({
                    "symbol": symbol,
                    "name": result.get("name"),
                    "reason": _sell_candidate_wait_reason(
                        result=result,
                        score=score,
                        threshold=threshold,
                        triggers=triggers,
                    ),
                    "discipline_review": {},
                    "alert_source": "committee_sell",
                })
            continue

        if verdict not in {"BUY", "ACCUMULATE"} or alloc <= 0:
            continue
        recent_trade = _recent_trade_for_symbol(symbol, recent_real_trades_by_symbol)
        wait_reason = _opposite_trade_wait_reason(
            direction="BUY",
            triggers=triggers,
            current_price=price,
            recent_trade=recent_trade,
        )
        if wait_reason:
            suppressed.append({
                "symbol": symbol,
                "name": result.get("name"),
                "reason": wait_reason,
                "recent_trade": recent_trade,
                "alert_source": "trade_cooldown",
            })
            continue
        if _is_limit_up_buy_blocked(result, price_info):
            suppressed.append({"symbol": symbol, "name": result.get("name"), "reason": "limit_up_buy_blocked"})
            continue
        effective_buy_threshold = round(buy_threshold, 2)
        if score < effective_buy_threshold:
            suppressed.append({
                "symbol": symbol,
                "name": result.get("name"),
                "reason": f"low_score:{score:.1f}<{effective_buy_threshold:.1f}:{mode}",
            })
            continue

        lot = int(stock.get("min_lot_size") or result.get("min_lot_size") or 100)
        lot = max(lot, 1)
        lot_cost = price * lot
        suggested_lots = int(abs(alloc) // lot_cost)
        affordable_lots = int(max(buy_cash_budget, 0.0) // lot_cost)
        max_lots = min(max(suggested_lots, 1), affordable_lots)
        if buy_lot_multiplier < 1.0:
            max_lots = max(1, int(math.floor(max_lots * buy_lot_multiplier))) if max_lots > 0 else 0
        if max_lots <= 0:
            reason = "cash_reserve_insufficient" if buy_cash_budget < cash else "cash_insufficient"
            suppressed.append({
                "symbol": symbol,
                "name": result.get("name"),
                "reason": f"{reason}:{mode}",
            })
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
            math_review = _math_review_position_scale(
                result,
                stock=stock,
                price=price,
                lots=lots,
                lot_size=lot,
                portfolio_value=base_portfolio_value,
                alert_score=score,
                max_single_position_pct=max_single_position_pct,
            )
            option = dict(result)
            option["suggested_alloc_cny"] = round(cost)
            option["optimizer_lots"] = lots
            option["alert_selected_lots"] = lots
            option["llm_review_lots"] = math_review["recommended_lots"]
            option["llm_position_scale"] = math_review["position_scale"]
            option["llm_position_scale_value"] = math_review["position_scale_value"]
            option["llm_position_scale_multiplier"] = math_review["position_scale_multiplier"]
            option["llm_risk_components"] = math_review["risk_components"]
            option["alert_score"] = round(score, 2)
            option["alert_raw_value"] = round(base_value, 4)
            option["alert_risk_penalty"] = round(risk_penalty, 4)
            option["alert_value"] = round(base_value - risk_penalty + buy_value_adjustment, 4)
            option["trading_mode"] = mode
            option["trading_mode_label"] = mode_label
            option["alert_buy_threshold"] = buy_threshold
            option["effective_buy_threshold"] = effective_buy_threshold
            option["alert_cash_reserve_cny"] = round(reserve_cash, 2)
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
    budget = int(max(buy_cash_budget, 0.0) // budget_unit)
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
