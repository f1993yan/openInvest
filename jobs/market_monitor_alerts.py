"""Cash/risk aware alert selection for market monitor."""
from __future__ import annotations

import math
import re
import statistics
from typing import Any, Dict, List, Optional, Tuple

from jobs.market_monitor_common import _clamp, _safe_num
from jobs.market_monitor_entry_exit import (
    evaluate_entry_exit_triggers,
    evaluate_position_exit_plan_triggers,
    _stock_units,
    _update_position_exit_plan,
)
from jobs.market_monitor_guards import _is_limit_up_buy_blocked
from jobs.market_monitor_guards import _trigger_has_price_progress_from_trade
from jobs.trading_mode import (
    DEFAULT_TRADING_MODE,
    TRADE_MODE_RISK_OFF,
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
SELL_ALERT_POLICY_FLOOR = 41.0
SELL_ALERT_COMMITTEE_FLOOR = 34.0
BUY_ALERT_THRESHOLD = 55.0
DISCIPLINE_REVIEW_ALERT_SOURCE = "position_exit_discipline_review"
DISCIPLINE_EXECUTION_FRICTION_PCT = 0.15
SECTOR_PANIC_MIN_SAMPLE = 3
SECTOR_PANIC_DOWN_RATIO = 2 / 3
SECTOR_PANIC_HARD_DOWN_RATIO = 0.20
SECTOR_PANIC_MIN_MEDIAN_DROP_PCT = 3.0
SECTOR_PANIC_MIN_EXCESS_DROP_PCT = 1.0
SECTOR_PANIC_TARGET_WEAK_Z = -1.0


def _entry_exit_atr_pct(result: Dict[str, Any]) -> float:
    ee = result.get("entry_exit_points") or {}
    tech = result.get("technical") or {}
    return _clamp(
        max(
            _safe_num(ee.get("atr_pct")),
            _safe_num(result.get("atr_pct")),
            _safe_num(result.get("volatility_pct")),
            _safe_num(tech.get("atr_pct")),
        ),
        0.0,
        20.0,
    )


def _regime_volatility_gate(result: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic regime/noise gate for alert thresholds.

    This implements the conservative lesson from recent walk-forward papers:
    raw return forecasts are weak, so execution should depend on regime and
    volatility.  The gate only adjusts alert thresholds and lots; it never
    changes committee verdicts or price trigger lines.
    """
    text = f"{result.get('regime', '')}\n{result.get('market_data', '')}\n{result.get('quant_view', '')}".lower()
    atr_pct = _entry_exit_atr_pct(result)
    is_crash = "crash" in text or "panic" in text or "崩" in text or "恐慌" in text
    is_downtrend = "downtrend" in text or "bear" in text or "下行" in text or "空头" in text
    is_uptrend = "uptrend" in text or "recovery" in text or "上行" in text or "多头" in text or "修复" in text
    high_noise = is_crash or atr_pct >= 4.5 or (is_downtrend and atr_pct >= 3.2)
    low_noise = (is_uptrend or "range_bound" in text or "震荡" in text) and 0.5 <= atr_pct <= 2.8
    if high_noise:
        return {
            "state": "high_noise",
            "atr_pct": round(atr_pct, 4),
            "buy_threshold_adjustment": 8.0 if not is_crash else 10.0,
            "buy_lot_multiplier": 0.5,
            "sell_threshold_adjustment": 4.0,
            "reason": "高波动/下行状态：买入收紧，委员会单独卖出需要更强确认",
        }
    if low_noise:
        return {
            "state": "low_noise",
            "atr_pct": round(atr_pct, 4),
            "buy_threshold_adjustment": -2.0,
            "buy_lot_multiplier": 1.0,
            "sell_threshold_adjustment": 0.0,
            "reason": "低噪声趋势/震荡状态：允许价格触发买点略微放宽",
        }
    return {
        "state": "normal",
        "atr_pct": round(atr_pct, 4),
        "buy_threshold_adjustment": 0.0,
        "buy_lot_multiplier": 1.0,
        "sell_threshold_adjustment": 0.0,
        "reason": "常规波动状态",
    }


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


def _position_policy_quality_adjustment(result: Dict[str, Any], stock: Dict[str, Any]) -> float:
    """Small likelihood-ratio adjustment for existing-position sell discipline.

    The policy quality score is already a risk-adjusted expected utility from
    weekly walk-forward backtests.  We only use it for held TRIM/SELL candidates
    and shrink it by sell sample size, so it cannot turn a weak/no-trigger idea
    into an automatic trade by itself.
    """
    verdict = str(result.get("verdict", "")).upper()
    if verdict not in {"TRIM", "SELL"}:
        return 0.0
    if _safe_num(stock.get("position_pct")) <= 0 and _safe_num(stock.get("units")) <= 0:
        return 0.0
    policy = result.get("position_exit_policy") or {}
    quality = _safe_num(policy.get("policy_quality_score"))
    sell_count = max(0.0, _safe_num(policy.get("sell_count")))
    win_lower = _safe_num(policy.get("sell_win_rate_lower"))
    expectancy = _safe_num(policy.get("conservative_sell_expectancy_cny"))
    reliability = sell_count / (sell_count + 8.0) if sell_count > 0 else 0.0
    if reliability <= 0:
        return 0.0
    utility_edge = math.tanh(quality / 4.0)
    expectancy_edge = math.tanh(expectancy / 500.0)
    win_edge = max(-0.5, min(0.5, win_lower - 0.5))
    log_lr = reliability * (0.22 * utility_edge + 0.12 * expectancy_edge + 0.08 * win_edge)
    return _clamp(_LLM_REVIEW_LOGIT_SCALE * log_lr, -4.0, 4.0)


def _sell_policy_reliability(policy: Dict[str, Any]) -> float:
    explicit = _safe_num(policy.get("sell_reliability"))
    if explicit > 0:
        return _clamp(explicit, 0.0, 1.0)
    sell_count = max(0.0, _safe_num(policy.get("sell_count")))
    return sell_count / (sell_count + 8.0) if sell_count > 0 else 0.0


def _sell_policy_edge(result: Dict[str, Any]) -> float:
    """Shrunk conservative expected utility for sell alerts.

    Utility is estimated directly from the sell outcome distribution:

        U = p_lower * avg_win - (1 - p_lower) * avg_loss

    where ``p_lower`` is the Wilson lower bound, not the raw win rate.  The
    result is normalized by payoff scale and shrunk by sample reliability, so a
    tiny sample with a lucky 100% win rate cannot dominate alert selection.
    """
    policy = result.get("position_exit_policy") or {}
    reliability = _sell_policy_reliability(policy)
    if reliability <= 0:
        return 0.0
    win_lower = _safe_num(policy.get("sell_win_rate_lower"))
    avg_win = _safe_num(policy.get("avg_sell_win_cny"))
    avg_loss = _safe_num(policy.get("avg_sell_loss_cny"))
    expectancy = _safe_num(policy.get("conservative_sell_expectancy_cny"))
    path_edge = _safe_num(policy.get("avg_post_sell_net_edge_pct"))
    avoided = _safe_num(policy.get("avg_post_sell_avoided_drawdown_pct"))
    missed = _safe_num(policy.get("avg_post_sell_missed_rebound_pct"))
    path_scale = max(avoided + missed, abs(path_edge), 1.0)
    if avg_win > 0 or avg_loss > 0:
        expectancy = win_lower * avg_win - (1.0 - win_lower) * avg_loss
    payoff_scale = max(avg_win, avg_loss, abs(expectancy), 100.0)
    normalized_utility = _clamp(expectancy / payoff_scale, -1.0, 1.0)
    path_utility = _clamp(path_edge / path_scale, -1.0, 1.0)
    return reliability * (0.75 * normalized_utility + 0.25 * path_utility)


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
    - weekly walk-forward sell policy edge using Wilson-lower win rate;
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
    policy_edge = _clamp(_sell_policy_edge(result), -1.0, 1.0)
    change_pct = _safe_num(price_info.get("change_pct"))
    price_edge = _clamp((-change_pct) / 5.0, -0.4, 0.8)
    raw = (
        0.36 * (confidence * direction_edge)
        + 0.28 * pressure
        + 0.24 * max(0.0, policy_edge)
        + 0.12 * max(0.0, price_edge)
    )
    return _clamp(raw, 0.0, 1.0)


def _sell_alert_threshold(result: Dict[str, Any], triggers: List[Dict[str, Any]]) -> float:
    """Minimum score for held TRIM/SELL alerts.

    The threshold is intentionally lower for confirmed stop/take-profit
    triggers because those are executable price events.  Committee-only sell
    ideas still need stronger evidence, but a sector policy with positive
    Wilson-lower-bound expectancy can lower the bar modestly.
    """
    has_sell_trigger = any(t.get("side") == "sell" for t in triggers)
    threshold = SELL_ALERT_BASE_THRESHOLD
    if has_sell_trigger:
        threshold = SELL_ALERT_TRIGGER_FLOOR

    edge = _sell_policy_edge(result)
    # Same logit-score scale used for LLM/review evidence.  Positive expected
    # utility reduces the alert threshold; negative expected utility raises it.
    utility_adjustment = _clamp(_LLM_REVIEW_LOGIT_SCALE * edge, -5.0, 4.0)
    if edge > 0:
        threshold -= utility_adjustment
    elif edge < 0:
        threshold += abs(utility_adjustment)

    floor = SELL_ALERT_TRIGGER_FLOOR if has_sell_trigger else SELL_ALERT_POLICY_FLOOR
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
        policy_edge=_sell_policy_edge(result),
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
    if mode == TRADE_MODE_RISK_OFF:
        threshold += _safe_num(_regime_volatility_gate(result).get("sell_threshold_adjustment"))
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


def _sell_trigger_kind(triggers: List[Dict[str, Any]]) -> str:
    priority = {
        "position_stop": 0,
        "cost_stop_loss": 1,
        "stop_loss": 2,
        "take_profit_2": 3,
        "take_profit": 4,
        "take_profit_1": 5,
        "trim": 6,
    }
    sell_triggers = [t for t in triggers if t.get("side") == "sell"]
    if not sell_triggers:
        return ""
    first = min(sell_triggers, key=lambda t: priority.get(str(t.get("kind") or ""), 99))
    return str(first.get("kind") or "")


def _discipline_trigger_strength(triggers: List[Dict[str, Any]]) -> float:
    kind = _sell_trigger_kind(triggers)
    return {
        "position_stop": 1.0,
        "cost_stop_loss": 1.0,
        "stop_loss": 0.92,
        "take_profit_2": 0.78,
        "take_profit": 0.66,
        "take_profit_1": 0.56,
        "trim": 0.48,
    }.get(kind, 0.0)


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
    return "discipline_review_no_sellable_lots"


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


def _confirmed_position_sell_trigger(
    *,
    symbol: str,
    entry_exit_state: Dict[str, Any],
    current_triggers: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    sell_triggers = [t for t in current_triggers if t.get("side") == "sell"]
    if not sell_triggers:
        return False, "no_sell_trigger"
    kind = _sell_trigger_kind(sell_triggers)
    if kind in {"position_stop", "cost_stop_loss", "stop_loss"}:
        return True, "immediate_stop"
    previous = ((entry_exit_state.get("symbols") or {}).get(symbol) or {})
    previous_triggers = previous.get("last_triggers") or []
    previous_sell = any(t.get("side") == "sell" for t in previous_triggers)
    return (True, "two_round_price_confirmed") if previous_sell else (False, "one_round_only")


def _discipline_continuation_edge_pct(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    price_info: Dict[str, Any],
) -> float:
    """Expected benefit of continuing to hold, in percentage-point units.

    The inputs are deliberately limited to existing stable committee fields.
    Positive values make a sell discipline trigger require stronger evidence;
    negative values make reducing risk easier.
    """
    verdict = str(result.get("verdict", "")).upper()
    confidence = _clamp(_safe_num(result.get("confidence")), 0.0, 1.0)
    ee = result.get("entry_exit_points") or {}
    expected_return = _clamp(_safe_num(ee.get("expected_return_pct")), -8.0, 8.0)
    rr = _safe_num(ee.get("reward_risk_ratio"))
    fundamental = _safe_num(result.get("fundamental_score"), 50.0)
    right_gate = result.get("right_side_trend_gate") or {}
    change_pct = _safe_num(price_info.get("change_pct"))
    position_pct = _safe_num(stock.get("position_pct"))

    verdict_edge = {
        "BUY": 2.4,
        "ACCUMULATE": 1.6,
        "HOLD": 0.7,
        "WAIT": 0.3,
        "TRIM": -0.8,
        "SELL": -1.4,
    }.get(verdict, 0.0) * confidence
    expected_edge = 0.22 * expected_return
    rr_edge = _clamp((rr - 1.5) * 0.35, -0.6, 0.8) if rr > 0 else 0.0
    fundamental_edge = _clamp((fundamental - 60.0) / 40.0, -0.5, 0.8)
    gate_edge = 0.45 if right_gate.get("allow") else -0.35 if right_gate else 0.0
    intraday_edge = _clamp(change_pct / 10.0, -0.4, 0.5)
    concentration_drag = _clamp((position_pct - 18.0) / 20.0, 0.0, 0.5)
    return round(
        _clamp(
            verdict_edge + expected_edge + rr_edge + fundamental_edge + gate_edge + intraday_edge - concentration_drag,
            -2.5,
            4.0,
        ),
        4,
    )


def _discipline_sell_expected_edge_pct(
    result: Dict[str, Any],
    *,
    triggers: List[Dict[str, Any]],
) -> float:
    """Conservative sell-side expected utility for triggered exit discipline.

    Stop/take-profit lines are not treated as certainties.  Their base evidence
    is multiplied by the weekly policy reliability and combined with the
    Wilson-lower sell/path evidence already written by the optimizer.
    """
    policy = result.get("position_exit_policy") or {}
    kind = _sell_trigger_kind(triggers)
    strength = _discipline_trigger_strength(triggers)
    reliability = _sell_policy_reliability(policy)
    win_lower = _safe_num(policy.get("sell_win_rate_lower"))
    path_lower = _safe_num(policy.get("post_sell_positive_edge_lower"))
    utility_adjustment = _safe_num(policy.get("sell_utility_adjustment_pct"))
    path_edge = _safe_num(policy.get("avg_post_sell_net_edge_pct"))
    max_loss = max(0.0, _safe_num(policy.get("max_loss_pct")))

    conservative_probability = _clamp(max(win_lower, path_lower, 0.5) - 0.5, 0.0, 0.5) * 2.0
    probability_edge = reliability * conservative_probability
    path_utility = reliability * _clamp(max(path_edge, utility_adjustment) / 8.0, -1.0, 1.0)

    if kind in {"position_stop", "cost_stop_loss", "stop_loss"}:
        base_edge = max(1.4, min(max_loss or 4.0, 8.0) * 0.45)
    elif kind in {"take_profit_2", "take_profit"}:
        base_edge = 0.55 + 1.05 * strength
    else:
        base_edge = 0.25 + 0.85 * strength
    reliability_weight = 0.55 + 0.45 * reliability
    return round(_clamp(base_edge * reliability_weight + 1.6 * path_utility + 0.8 * probability_edge, -2.0, 8.0), 4)


def _discipline_sell_review(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    price_info: Dict[str, Any],
    triggers: List[Dict[str, Any]],
    confirmed: bool,
    confirmation_reason: str,
    sector_panic_guard: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    policy = result.get("position_exit_policy") or {}
    kind = _sell_trigger_kind(triggers)
    panic_guard = dict(sector_panic_guard or {})
    sell_edge = _discipline_sell_expected_edge_pct(result, triggers=triggers)
    continuation_edge = _discipline_continuation_edge_pct(result, stock=stock, price_info=price_info)
    if panic_guard.get("active"):
        continuation_edge = round(
            continuation_edge + _safe_num(panic_guard.get("hold_utility_bonus_pct")),
            4,
        )
    friction = 0.0 if kind in {"position_stop", "cost_stop_loss", "stop_loss"} else DISCIPLINE_EXECUTION_FRICTION_PCT
    net_edge = sell_edge - continuation_edge - friction
    execute = bool(confirmed and net_edge > 0)
    if kind in {"position_stop", "cost_stop_loss", "stop_loss"} and confirmed:
        execute = net_edge > -1.0
    if panic_guard.get("active") and confirmed:
        execute = net_edge > _safe_num(panic_guard.get("required_edge_pct"), 0.35)
    return {
        "model": "triggered_exit_expected_utility_v1",
        "committee_verdict": str(result.get("verdict") or "").upper(),
        "committee_confidence": round(_safe_num(result.get("confidence")), 4),
        "trigger_kind": kind,
        "trigger_strength": round(_discipline_trigger_strength(triggers), 4),
        "confirmed": bool(confirmed),
        "confirmation_reason": confirmation_reason,
        "sell_expected_edge_pct": sell_edge,
        "continuation_edge_pct": continuation_edge,
        "execution_friction_pct": friction,
        "expected_utility_edge_pct": round(net_edge, 4),
        "policy_reliability": round(_sell_policy_reliability(policy), 4),
        "sell_win_rate_lower": round(_safe_num(policy.get("sell_win_rate_lower")), 4),
        "post_sell_positive_edge_lower": round(_safe_num(policy.get("post_sell_positive_edge_lower")), 4),
        "sell_utility_adjustment_pct": round(_safe_num(policy.get("sell_utility_adjustment_pct")), 4),
        "avg_post_sell_net_edge_pct": round(_safe_num(policy.get("avg_post_sell_net_edge_pct")), 4),
        "sector_panic_guard": panic_guard,
        "decision": "execute" if execute else "review",
    }


def _discipline_wait_reason(review: Dict[str, Any]) -> str:
    guard = review.get("sector_panic_guard") or {}
    if guard.get("active") and review.get("decision") != "execute":
        return (
            "sector_panic_guard:"
            f"{review.get('expected_utility_edge_pct', 0):.2f}:"
            f"{guard.get('sector', '')}:"
            f"{guard.get('panic_score', 0)}"
        )
    return (
        "discipline_review_wait:"
        f"{review.get('expected_utility_edge_pct', 0):.2f}:"
        f"{review.get('trigger_kind', '')}:"
        f"{review.get('confirmation_reason', '')}"
    )


def _held_lots(stock: Dict[str, Any], lot_size: int) -> int:
    units = _stock_units(stock)
    if units <= 0 or lot_size <= 0:
        return 0
    return max(1, int(math.floor(units / lot_size)))


def _discipline_sell_lots(kind: str, held_lots: int) -> int:
    if held_lots <= 0:
        return 0
    if kind in {"position_stop", "cost_stop_loss", "stop_loss", "take_profit_2", "take_profit"}:
        return held_lots
    return max(1, int(math.floor(held_lots * 0.5)))


def _build_discipline_sell_candidate(
    result: Dict[str, Any],
    *,
    stock: Dict[str, Any],
    price_info: Dict[str, Any],
    triggers: List[Dict[str, Any]],
    entry_exit_state: Dict[str, Any],
    trading_mode: str,
    same_day_bought_units_by_symbol: Optional[Dict[str, float]] = None,
    sector_panic_context: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    symbol = str(result.get("symbol") or stock.get("symbol") or "").upper()
    if not symbol or not _is_existing_position(stock) or not any(t.get("side") == "sell" for t in triggers):
        return None
    confirmed, confirmation_reason = _confirmed_position_sell_trigger(
        symbol=symbol,
        entry_exit_state=entry_exit_state,
        current_triggers=triggers,
    )
    review = _discipline_sell_review(
        result,
        stock=stock,
        price_info=price_info,
        triggers=triggers,
        confirmed=confirmed,
        confirmation_reason=confirmation_reason,
        sector_panic_guard=_sector_panic_guard_for_result(
            result,
            stock,
            price_info,
            sector_panic_context,
        ),
    )
    kind = str(review.get("trigger_kind") or "")
    lot_size = _alert_lot_size(stock, result)
    sellable_units = _sellable_units_for_alert(
        result,
        stock=stock,
        same_day_bought_units_by_symbol=same_day_bought_units_by_symbol,
    )
    held_lots = int(math.floor(sellable_units / lot_size)) if sellable_units > 0 else 0
    sell_lots = _discipline_sell_lots(kind, held_lots)
    price = _safe_num(price_info.get("price"))
    if sell_lots <= 0 or price <= 0:
        review["decision"] = "review"
        return {
            **dict(result),
            "alert_source": DISCIPLINE_REVIEW_ALERT_SOURCE,
            "discipline_review": review,
            "alert_triggers": triggers,
            "alert_wait_reason": _same_day_t1_block_reason(
                result,
                stock=stock,
                same_day_bought_units_by_symbol=same_day_bought_units_by_symbol,
            ),
        }
    verdict = "SELL" if kind in {"position_stop", "cost_stop_loss", "stop_loss", "take_profit_2", "take_profit"} else "TRIM"
    confidence = _clamp(
        0.34
        + 0.26 * _discipline_trigger_strength(triggers)
        + 0.035 * max(0.0, _safe_num(review.get("expected_utility_edge_pct"))),
        0.25,
        0.86,
    )
    candidate = dict(result)
    candidate.update({
        "verdict": verdict,
        "committee_verdict": str(result.get("verdict") or "").upper(),
        "confidence": max(_safe_num(result.get("confidence")), confidence),
        "suggested_alloc_cny": round(-(sell_lots * lot_size * price), 2),
        "optimizer_lots": sell_lots,
        "alert_selected_lots": sell_lots,
        "llm_review_lots": sell_lots,
        "alert_source": DISCIPLINE_REVIEW_ALERT_SOURCE,
        "discipline_review": review,
        "alert_triggers": triggers,
        "trading_mode": normalize_trading_mode(trading_mode),
    })
    return candidate


def _sell_candidate_wait_label(reason: str) -> str:
    text = str(reason or "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)<([0-9]+(?:\.[0-9]+)?)", text)
    if "discipline_review_wait" in text:
        parts = text.split(":")
        kind = parts[2] if len(parts) > 2 else ""
        if "one_round_only" in text:
            base = "纪律线单轮触发，等待下一轮确认"
        elif kind in {"position_stop", "cost_stop_loss", "stop_loss"}:
            base = "止损纪律触发，但继续持有证据仍占优"
        else:
            base = "止盈纪律复核，卖出期望未明显胜过继续持有"
        edge_match = re.search(r"discipline_review_wait:([-0-9.]+)", text)
        if edge_match:
            return f"{base}（净效用{edge_match.group(1)}pct）"
        return base
    if "sector_panic_guard" in text:
        parts = text.split(":")
        edge = parts[1] if len(parts) > 1 else ""
        sector = parts[2] if len(parts) > 2 else ""
        base = "板块共振杀跌，暂缓机械止损"
        if sector:
            base = f"{sector}板块共振杀跌，暂缓机械止损"
        return f"{base}（净效用{edge}pct）" if edge else base
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
        position_exit_plan = _update_position_exit_plan(
            previous.get("position_exit_plan"),
            symbol=symbol,
            stock=stock,
            result=result,
            current_price=current_price,
            is_holding=True,
        )
        return evaluate_position_exit_plan_triggers(
            current_price,
            position_exit_plan,
        )
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
    score += _position_policy_quality_adjustment(result, stock)
    return score


def _stock_sector(stock: Dict[str, Any]) -> str:
    return str(stock.get("sector") or stock.get("industry") or "UNKNOWN")


def _existing_sector_exposure(stocks: List[Dict[str, Any]]) -> Dict[str, float]:
    exposure: Dict[str, float] = {}
    for stock in stocks:
        sector = _stock_sector(stock)
        exposure[sector] = exposure.get(sector, 0.0) + _safe_num(stock.get("position_pct"))
    return exposure


def _robust_scale(values: List[float]) -> float:
    if len(values) < 2:
        return 1.0
    center = statistics.median(values)
    deviations = [abs(v - center) for v in values]
    mad = statistics.median(deviations)
    if mad > 0:
        return max(1.0, 1.4826 * mad)
    try:
        return max(1.0, statistics.pstdev(values))
    except statistics.StatisticsError:
        return 1.0


def _price_change_pct_for_symbol(symbol: str, prices: Dict[str, Dict[str, Any]]) -> Optional[float]:
    row = prices.get(symbol.upper()) or prices.get(symbol)
    if not row:
        return None
    value = row.get("change_pct")
    if value is None or value == "":
        return None
    change = _safe_num(value)
    if not math.isfinite(change):
        return None
    return change


def _build_sector_panic_context(
    *,
    stocks: List[Dict[str, Any]],
    prices: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Detect broad sector capitulation from the current monitored universe.

    The guard intentionally uses robust cross-sectional statistics instead of a
    single hard price line: a sector must fall together, fall more than the
    monitored market median, and the held stock must not be materially weaker
    than its own sector.  With fewer than three valid sector samples the guard
    is disabled because the math would be too noisy.
    """
    changes_by_symbol: Dict[str, float] = {}
    sector_members: Dict[str, List[Tuple[str, float]]] = {}
    market_changes: List[float] = []
    for stock in stocks:
        symbol = str(stock.get("symbol") or "").upper()
        if not symbol:
            continue
        change = _price_change_pct_for_symbol(symbol, prices)
        if change is None:
            continue
        sector = _stock_sector(stock)
        changes_by_symbol[symbol] = change
        sector_members.setdefault(sector, []).append((symbol, change))
        market_changes.append(change)

    if not market_changes:
        return {}
    market_median = statistics.median(market_changes)
    context: Dict[str, Dict[str, Any]] = {}
    for sector, members in sector_members.items():
        if sector == "UNKNOWN" or len(members) < SECTOR_PANIC_MIN_SAMPLE:
            continue
        changes = [change for _, change in members]
        sector_median = statistics.median(changes)
        sector_scale = _robust_scale(changes)
        down_ratio = sum(1 for change in changes if change <= -2.5) / len(changes)
        hard_down_ratio = sum(1 for change in changes if change <= -5.0) / len(changes)
        excess_drop_pct = max(0.0, market_median - sector_median)
        is_sector_panic = (
            sector_median <= -SECTOR_PANIC_MIN_MEDIAN_DROP_PCT
            and down_ratio >= SECTOR_PANIC_DOWN_RATIO
            and (
                hard_down_ratio >= SECTOR_PANIC_HARD_DOWN_RATIO
                or excess_drop_pct >= SECTOR_PANIC_MIN_EXCESS_DROP_PCT
            )
        )
        if not is_sector_panic:
            continue
        panic_score = _clamp(
            0.35 * min(abs(sector_median) / 6.0, 1.0)
            + 0.30 * down_ratio
            + 0.20 * min(hard_down_ratio / 0.50, 1.0)
            + 0.15 * min(excess_drop_pct / 4.0, 1.0),
            0.0,
            1.0,
        )
        for symbol, change in members:
            relative_z = (change - sector_median) / sector_scale
            context[symbol] = {
                "active": relative_z >= SECTOR_PANIC_TARGET_WEAK_Z,
                "sector": sector,
                "sample_count": len(members),
                "market_median_change_pct": round(market_median, 4),
                "sector_median_change_pct": round(sector_median, 4),
                "sector_down_ratio": round(down_ratio, 4),
                "sector_hard_down_ratio": round(hard_down_ratio, 4),
                "sector_excess_drop_pct": round(excess_drop_pct, 4),
                "target_change_pct": round(change, 4),
                "target_relative_z": round(relative_z, 4),
                "panic_score": round(panic_score, 4),
                "reason": "sector_capitulation_not_idiosyncratic"
                if relative_z >= SECTOR_PANIC_TARGET_WEAK_Z
                else "target_weaker_than_sector",
            }
    return context


def _sector_panic_guard_for_result(
    result: Dict[str, Any],
    stock: Dict[str, Any],
    price_info: Dict[str, Any],
    sector_panic_context: Optional[Dict[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    symbol = str(result.get("symbol") or stock.get("symbol") or "").upper()
    base = dict((sector_panic_context or {}).get(symbol) or {})
    if not base:
        return {}
    if not base.get("active"):
        return {**base, "hold_utility_bonus_pct": 0.0, "required_edge_pct": 0.0}

    verdict = str(result.get("verdict") or "").upper()
    confidence = _safe_num(result.get("confidence"))
    price = _safe_num(price_info.get("price"))
    pressure = _sell_position_pressure(result, stock, price)
    if verdict == "SELL" and confidence >= 0.72 and pressure >= 0.45:
        return {
            **base,
            "active": False,
            "reason": "committee_hard_sell_overrides_sector_panic_guard",
            "hold_utility_bonus_pct": 0.0,
            "required_edge_pct": 0.0,
            "sell_pressure": round(pressure, 4),
        }

    policy = result.get("position_exit_policy") or {}
    reliability = _sell_policy_reliability(policy)
    missed_rebound = max(0.0, _safe_num(policy.get("avg_post_sell_missed_rebound_pct")))
    panic_score = _safe_num(base.get("panic_score"))
    sector_excess = _safe_num(base.get("sector_excess_drop_pct"))
    sector_median = abs(_safe_num(base.get("sector_median_change_pct")))
    hard_down_ratio = _safe_num(base.get("sector_hard_down_ratio"))

    shock_cost = 0.35 * sector_excess + 0.12 * sector_median * hard_down_ratio
    rebound_cost = reliability * min(missed_rebound, 6.0)
    hold_bonus = _clamp(panic_score * (shock_cost + 0.45 * rebound_cost), 0.0, 3.5)
    required_edge = _clamp(0.15 + 0.35 * panic_score + 0.10 * hard_down_ratio, 0.15, 0.65)
    return {
        **base,
        "hold_utility_bonus_pct": round(hold_bonus, 4),
        "required_edge_pct": round(required_edge, 4),
        "sell_pressure": round(pressure, 4),
        "policy_missed_rebound_pct": round(missed_rebound, 4),
        "policy_reliability": round(reliability, 4),
    }


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
    sector_panic_context = _build_sector_panic_context(stocks=stocks, prices=prices)
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
        discipline_candidate = _build_discipline_sell_candidate(
            result,
            stock=stock,
            price_info=price_info,
            triggers=triggers,
            entry_exit_state=entry_exit_state,
            trading_mode=mode,
            same_day_bought_units_by_symbol=same_day_bought_units_by_symbol,
            sector_panic_context=sector_panic_context,
        )

        if verdict in {"HOLD", "REDUCE", "UNCLEAR"} or abs(alloc) <= 0:
            if discipline_candidate:
                review = discipline_candidate.get("discipline_review") or {}
                if review.get("decision") == "execute":
                    score = _action_score(
                        discipline_candidate,
                        stock=stock,
                        price_info=price_info,
                        triggers=triggers,
                    )
                    score += 4.0 * max(0.0, _safe_num(review.get("expected_utility_edge_pct")))
                    threshold = _sell_committee_alert_threshold(
                        discipline_candidate,
                        stock=stock,
                        price_info=price_info,
                        triggers=triggers,
                        trading_mode=mode,
                    )
                    if score >= threshold:
                        selected = dict(discipline_candidate)
                        selected["alert_score"] = round(score, 2)
                        selected["alert_threshold"] = threshold
                        selected["trading_mode_label"] = mode_label
                        sell_alerts.append(selected)
                    else:
                        suppressed.append({
                            "symbol": symbol,
                            "name": result.get("name"),
                            "reason": _sell_candidate_wait_reason(
                                result=discipline_candidate,
                                score=score,
                                threshold=threshold,
                                triggers=triggers,
                            ),
                            "discipline_review": review,
                            "alert_source": DISCIPLINE_REVIEW_ALERT_SOURCE,
                        })
                else:
                    suppressed.append({
                        "symbol": symbol,
                        "name": result.get("name"),
                        "reason": discipline_candidate.get("alert_wait_reason") or _discipline_wait_reason(review),
                        "discipline_review": review,
                        "alert_source": DISCIPLINE_REVIEW_ALERT_SOURCE,
                    })
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
            if not any(t.get("side") == "sell" for t in triggers):
                suppressed.append({
                    "symbol": symbol,
                    "name": result.get("name"),
                    "reason": f"sell_waiting_for_current_exit_trigger:{verdict}",
                    "discipline_review": (discipline_candidate or {}).get("discipline_review") or {},
                    "alert_source": (discipline_candidate or {}).get("alert_source") or "committee_sell",
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
                    "discipline_review": (discipline_candidate or {}).get("discipline_review") or {},
                    "alert_source": (discipline_candidate or {}).get("alert_source") or "committee_sell",
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
                if discipline_candidate:
                    selected["discipline_review"] = discipline_candidate.get("discipline_review") or {}
                    selected.setdefault("alert_source", "committee_sell")
                    selected["optimizer_lots"] = min(
                        int(_safe_num(discipline_candidate.get("optimizer_lots") or sellable_lots)),
                        sellable_lots,
                    )
                    selected["alert_selected_lots"] = selected["optimizer_lots"]
                    selected["llm_review_lots"] = min(
                        int(_safe_num(discipline_candidate.get("llm_review_lots") or selected["optimizer_lots"])),
                        selected["optimizer_lots"],
                    )
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
                    "discipline_review": (discipline_candidate or {}).get("discipline_review") or {},
                    "alert_source": (discipline_candidate or {}).get("alert_source") or "committee_sell",
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
        regime_gate = _regime_volatility_gate(result) if mode == TRADE_MODE_RISK_OFF else {
            "state": "disabled",
            "atr_pct": _entry_exit_atr_pct(result),
            "buy_threshold_adjustment": 0.0,
            "buy_lot_multiplier": 1.0,
            "sell_threshold_adjustment": 0.0,
            "reason": "仅主动避险模式启用 regime/波动门控",
        }
        effective_buy_threshold = round(
            buy_threshold + _safe_num(regime_gate.get("buy_threshold_adjustment")),
            2,
        )
        if score < effective_buy_threshold:
            suppressed.append({
                "symbol": symbol,
                "name": result.get("name"),
                "reason": f"low_score:{score:.1f}<{effective_buy_threshold:.1f}:{mode}:{regime_gate.get('state')}",
                "regime_volatility_gate": regime_gate,
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
        gate_lot_multiplier = _safe_num(regime_gate.get("buy_lot_multiplier"), 1.0)
        if gate_lot_multiplier < 1.0:
            max_lots = max(1, int(math.floor(max_lots * gate_lot_multiplier))) if max_lots > 0 else 0
        if max_lots <= 0:
            reason = "cash_reserve_insufficient" if buy_cash_budget < cash else "cash_insufficient"
            suppressed.append({
                "symbol": symbol,
                "name": result.get("name"),
                "reason": f"{reason}:{mode}",
                "regime_volatility_gate": regime_gate,
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
            option["regime_volatility_gate"] = regime_gate
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
