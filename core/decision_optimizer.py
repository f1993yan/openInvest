"""Deterministic execution optimizer for committee verdicts.

The LLM committee is useful for narrative and weak priors, but executable
allocation must be chosen by a deterministic objective. This module enumerates
lot-sized actions and chooses the one with the highest expected utility versus
HOLD under cash, lot, concentration, cost, and risk constraints.

Model references for the objective used below:
- Mean-variance utility / quadratic risk penalty:
  Markowitz, H. (1952), "Portfolio Selection", Journal of Finance.
  https://doi.org/10.1111/j.1540-6261.1952.tb01525.x
- Black-Litterman strategic anchor:
  Black, F. and Litterman, R. (1992), "Global Portfolio Optimization",
  Financial Analysts Journal. https://doi.org/10.2469/faj.v48.n5.28
- Kelly risk-budget term:
  Kelly, J. L. (1956), "A New Interpretation of Information Rate".
  https://doi.org/10.1109/TIT.1956.1056803
- CVaR tail-risk input:
  Rockafellar, R. T. and Uryasev, S. (2000), "Optimization of
  Conditional Value-at-Risk". https://doi.org/10.21314/JOR.2000.038
- Regime-conditioned expected return idea:
  Hamilton, J. D. (1989), "A New Approach to the Economic Analysis of
  Nonstationary Time Series and the Business Cycle".
  https://doi.org/10.2307/1912559
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

MAX_EXACT_LOT_ENUM = int(os.getenv("INVEST_OPTIMIZER_MAX_EXACT_LOT_ENUM", "2000"))


@dataclass(frozen=True)
class OptimizedDecision:
    verdict: str
    alloc_cny: int
    confidence: float
    lots: int
    hand_cost: float
    edge_cny: float
    expected_return_pct: float
    sigma_30d_pct: float
    p_directional: float
    odds: float
    kelly_fraction: float
    target_position_pct: float
    cvar_95_loss_pct: float
    reason: str
    fundamental_score: float = 50.0
    fundamental_model: str = "none"
    fundamental_anchor_multiplier: float = 1.0
    fundamental_return_adjustment_pct: float = 0.0
    exit_policy_adjustment_pct: float = 0.0
    exit_policy_reliability: float = 0.0
    exit_policy_evidence_score: float = 0.0
    behavioral_factor_score: float = 0.0
    behavioral_target_weight_pct: float = 0.0
    behavioral_selected: bool = False
    behavioral_low_confidence: bool = True
    behavioral_model: str = "none"
    behavioral_optimizer_weight: float = 0.0
    behavioral_trailing_3m_return_pct: float = 0.0
    behavioral_trailing_3m_hit_rate: float = 0.5
    behavioral_trailing_3m_sample_size: int = 0

    def audit_text(self) -> str:
        side = "buy" if self.alloc_cny > 0 else "sell" if self.alloc_cny < 0 else "hold"
        return (
            "\n\n[OPTIMAL_DECISION]\n"
            f"side={side} verdict={self.verdict} lots={self.lots} "
            f"alloc_cny={self.alloc_cny} hand_cost={self.hand_cost:.2f}\n"
            f"edge_cny={self.edge_cny:.2f} expected_return_30d={self.expected_return_pct:+.2f}% "
            f"sigma_30d={self.sigma_30d_pct:.2f}% p={self.p_directional:.2f} "
            f"odds={self.odds:.2f} kelly={self.kelly_fraction:.2%}\n"
            f"black_litterman_anchor_target={self.target_position_pct:.2f}%\n"
            f"fundamental_model={self.fundamental_model} score={self.fundamental_score:.1f} "
            f"anchor_multiplier={self.fundamental_anchor_multiplier:.3f} "
            f"return_adj_30d={self.fundamental_return_adjustment_pct:+.2f}%\n"
            f"exit_policy_adjustment_30d={self.exit_policy_adjustment_pct:+.2f}% "
            f"exit_policy_reliability={self.exit_policy_reliability:.2f} "
            f"exit_policy_evidence={self.exit_policy_evidence_score:+.2f}\n"
            f"behavioral_model={self.behavioral_model} score={self.behavioral_factor_score:.2f} "
            f"selected={str(self.behavioral_selected).lower()} "
            f"target_weight={self.behavioral_target_weight_pct:.2f}% "
            f"low_confidence={str(self.behavioral_low_confidence).lower()} "
            f"optimizer_weight={self.behavioral_optimizer_weight:.3f} "
            f"trailing_3m_return={self.behavioral_trailing_3m_return_pct:+.2f}% "
            f"trailing_3m_hit_rate={self.behavioral_trailing_3m_hit_rate:.2%} "
            f"trailing_3m_n={self.behavioral_trailing_3m_sample_size}\n"
            f"conditional_cvar_95_loss={self.cvar_95_loss_pct:.2f}%\n"
            f"reason={self.reason}"
        )


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        v = float(x)
        return default if math.isnan(v) else v
    except (TypeError, ValueError):
        return default


def _sigmoid(x: float) -> float:
    if x >= 40:
        return 1.0
    if x <= -40:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _regime_label(regime_brief: str) -> str:
    for line in (regime_brief or "").splitlines():
        if line.startswith("REGIME:"):
            return line.split(":", 1)[1].strip()
    return ""


def _llm_prior_pct(verdict: str, confidence: float) -> float:
    """Weak directional prior from the committee, in 30d return percentage points."""
    direction = {
        "BUY": 2.0,
        "ACCUMULATE": 1.0,
        "HOLD": 0.0,
        "TRIM": -1.0,
        "SELL": -2.0,
    }.get((verdict or "").upper(), 0.0)
    return direction * _clamp(confidence, 0.0, 1.0)


def _estimate_expected_return_pct(
    *,
    parsed: Dict[str, Any],
    metrics: Dict[str, Any],
    regime: str,
    regime_probability: Optional[Any],
    conditional_return_stats: Optional[Any],
    fundamental_assessment: Optional[Any],
) -> float:
    """Estimate 30d expected return as a bounded model input.

    Preference order:
    1. Empirical OHLC regime probability mean return when available.
    2. Deterministic feature model from regime, momentum, quantile, RSI.
    3. Small LLM prior as tie-breaker, never as the full signal.
    """
    fundamental_adj = 0.0
    if fundamental_assessment is not None and not getattr(fundamental_assessment, "low_confidence", True):
        fundamental_adj = _clamp(
            _safe_float(getattr(fundamental_assessment, "expected_return_adjustment_pct", 0.0)),
            -1.5,
            1.5,
        )

    if (
        conditional_return_stats is not None
        and not getattr(conditional_return_stats, "low_confidence", False)
    ):
        return _clamp(
            _safe_float(getattr(conditional_return_stats, "mean_return_pct", 0.0)) + fundamental_adj,
            -15.0,
            15.0,
        )

    if regime_probability is not None and not getattr(regime_probability, "low_confidence", False):
        return _clamp(
            _safe_float(getattr(regime_probability, "mean_return", 0.0)) + fundamental_adj,
            -15.0,
            15.0,
        )

    regime_base = {
        "recovery": 3.0,
        "uptrend": 2.0,
        "range_bound": 0.0,
        "downtrend": -2.5,
        "crash": -5.0,
        "unknown": 0.0,
    }.get(regime, 0.0)

    ret30 = _safe_float(metrics.get("return_30d"), 0.0) * 100.0
    quantile = metrics.get("price_quantile_2y")
    rsi = metrics.get("rsi14")
    ma20 = metrics.get("ma20")
    ma120 = metrics.get("ma120")

    momentum = _clamp(ret30 * 0.15, -2.0, 2.0)
    trend = 0.0
    if ma20 and ma120:
        trend = _clamp(((float(ma20) - float(ma120)) / float(ma120)) * 40.0, -2.0, 2.0)

    mean_reversion = 0.0
    if quantile is not None:
        q = _safe_float(quantile, 0.5)
        if q <= 0.20:
            mean_reversion += 1.0
        elif q >= 0.80:
            mean_reversion -= 1.0

    rsi_adj = 0.0
    if rsi is not None:
        r = _safe_float(rsi, 50.0)
        if r < 35:
            rsi_adj += 0.8
        elif r > 70:
            rsi_adj -= 0.8

    llm = _llm_prior_pct(parsed.get("verdict", ""), _safe_float(parsed.get("confidence"), 0.0))
    return _clamp(
        regime_base + momentum + trend + mean_reversion + rsi_adj + llm + fundamental_adj,
        -15.0,
        15.0,
    )


def _exit_policy_adjustment_pct(
    *,
    position_exit_policy: Optional[Any],
    holding_value: float,
    total: float,
) -> tuple[float, float, float]:
    """Return a conservative 30d holding-return adjustment from sell evidence.

    The weekly optimizer computes this from two-month sector samples using
    Wilson-lower sell win rates and post-sell path edge.  We only apply it to
    existing positions; sparse or weak evidence shrinks to zero in
    ``PositionExitPolicy`` before it reaches this function.
    """
    if position_exit_policy is None or holding_value <= 0 or total <= 0:
        return 0.0, 0.0, 0.0
    raw = _clamp(_safe_float(getattr(position_exit_policy, "sell_utility_adjustment_pct", 0.0)), -8.0, 8.0)
    reliability = _clamp(_safe_float(getattr(position_exit_policy, "sell_reliability", 0.0)), 0.0, 1.0)
    evidence = _clamp(_safe_float(getattr(position_exit_policy, "sell_evidence_score", 0.0)), -1.0, 1.0)
    if reliability <= 0.0 or abs(raw) < 0.01:
        return 0.0, reliability, evidence
    exposure = _clamp(holding_value / total, 0.0, 1.0)
    # A tiny position should not dominate the whole optimizer.  The adjustment
    # is applied to the held asset's expected return and scaled by exposure
    # when that position is below a normal 20% sleeve.
    exposure_scale = _clamp(exposure / 0.20, 0.25, 1.0)
    return _clamp(raw * exposure_scale, -8.0, 8.0), reliability, evidence


def _sigma_30d_pct(metrics: Dict[str, Any]) -> float:
    # 30 个日历日的前瞻窗口 ≈ 21 个交易日。波动按 sqrt(交易日数) 缩放。
    # 用 21 而非 30：年化波动率本身以 252 个交易日为基，sqrt(21/252) 才是
    # 同一时间轴上的 30 日历日窗口；用 sqrt(30/252) 混了日历日/交易日，高估约 19%。
    horizon_td = 21.0
    vol_ann = metrics.get("volatility_annualized")
    if vol_ann is not None:
        return max(2.0, _safe_float(vol_ann) * math.sqrt(horizon_td / 252.0) * 100.0)
    # ATR-14 是**日度**真实波幅（%），把它缩放到 21 交易日应乘 sqrt(21)，
    # 而非 sqrt(30/14)。后者把 ATR 误当成 14 日累计波动，低估 ~sqrt(14)≈3.7 倍，
    # 进而低估 tail_sigma、放大仓位。仅在缺 volatility_annualized 时走此 fallback。
    atr_pct = _safe_float(metrics.get("atr_pct"), 2.0)
    return max(2.0, atr_pct * math.sqrt(horizon_td))


def _directional_probability(
    *,
    mu_pct: float,
    sigma_pct: float,
    regime_probability: Optional[Any],
    conditional_return_stats: Optional[Any],
    for_sell: bool,
) -> float:
    if (
        conditional_return_stats is not None
        and not getattr(conditional_return_stats, "low_confidence", False)
    ):
        p_up = _safe_float(getattr(conditional_return_stats, "p_up", 0.0))
        p_down = _safe_float(getattr(conditional_return_stats, "p_down", 0.0))
        p_flat = _safe_float(getattr(conditional_return_stats, "p_flat", 0.0))
        p = (p_down + 0.5 * p_flat) if for_sell else (p_up + 0.5 * p_flat)
        return _clamp(p, 0.05, 0.95)

    if regime_probability is not None and not getattr(regime_probability, "low_confidence", False):
        p_up = _safe_float(getattr(regime_probability, "p_up", 0.0))
        p_down = _safe_float(getattr(regime_probability, "p_down", 0.0))
        p_flat = _safe_float(getattr(regime_probability, "p_flat", 0.0))
        p = (p_down + 0.5 * p_flat) if for_sell else (p_up + 0.5 * p_flat)
        return _clamp(p, 0.05, 0.95)
    p_up = _sigmoid(mu_pct / max(sigma_pct, 1.0) * 2.0)
    return 1.0 - p_up if for_sell else p_up


def _candidate_lots(max_lots: int) -> Iterable[int]:
    if max_lots <= 0:
        return []
    if max_lots <= MAX_EXACT_LOT_ENUM:
        return range(1, max_lots + 1)
    base = {1, 2, 3, 5, 8, 13, max_lots}
    base.update(range(1, min(max_lots, 5) + 1))
    # For unusually low-priced names with very large feasible lot counts, keep
    # the search bounded but still cover the whole interval at regular points.
    step = max(1, max_lots // MAX_EXACT_LOT_ENUM)
    base.update(range(step, max_lots + 1, step))
    return sorted(x for x in base if 1 <= x <= max_lots)


def optimize_committee_decision(
    *,
    parsed: Dict[str, Any],
    metrics: Dict[str, Any],
    symbol: str,
    regime_brief: str,
    current_price: Optional[float],
    total_assets: float,
    available_cash: float,
    position_pct: float,
    min_lot_size: int,
    target_position_pct: Optional[float] = None,
    market: str = "a",
    risk_preference: str = "moderate",
    regime_probability: Optional[Any] = None,
    conditional_return_stats: Optional[Any] = None,
    bl_anchor_target_pct: Optional[float] = None,
    fundamental_assessment: Optional[Any] = None,
    position_exit_policy: Optional[Any] = None,
    behavioral_assessment: Optional[Any] = None,
) -> OptimizedDecision:
    """Choose the executable action with maximum expected utility vs HOLD.

    bl_anchor_target_pct: Black-Litterman 后验目标仓位百分比。非硬约束，
    短期 regime 边际收益足够时仍可偏离。
    """
    price = _safe_float(current_price or metrics.get("current_price"), 0.0)
    total = max(_safe_float(total_assets, 0.0), 1.0)
    cash = max(_safe_float(available_cash, 0.0), 0.0)
    lot_size = max(int(min_lot_size or 0), 1)
    hand_cost = price * lot_size if price > 0 else 0.0
    holding_value = max(total * _safe_float(position_pct, 0.0) / 100.0, 0.0)
    use_behavioral = (
        (market or "a").lower() == "a"
        and behavioral_assessment is not None
        and not getattr(behavioral_assessment, "low_confidence", True)
    )
    behavioral_target = _safe_float(
        getattr(behavioral_assessment, "target_weight_pct", 0.0), 0.0,
    ) if use_behavioral else None
    raw_target = (
        behavioral_target
        if behavioral_target is not None
        else bl_anchor_target_pct if bl_anchor_target_pct is not None else target_position_pct
    )
    bl_target = _safe_float(raw_target, _safe_float(position_pct, 0.0))
    fundamental_anchor_multiplier = 1.0
    fundamental_score = 50.0
    fundamental_model = "none"
    fundamental_return_adj = 0.0
    if fundamental_assessment is not None:
        fundamental_model = str(getattr(fundamental_assessment, "model_key", "unknown"))
        fundamental_score = _safe_float(getattr(fundamental_assessment, "score", 50.0), 50.0)
        if not getattr(fundamental_assessment, "low_confidence", True):
            fundamental_anchor_multiplier = _clamp(
                _safe_float(getattr(fundamental_assessment, "anchor_multiplier", 1.0), 1.0),
                0.75,
                1.25,
            )
            fundamental_return_adj = _clamp(
                _safe_float(getattr(fundamental_assessment, "expected_return_adjustment_pct", 0.0), 0.0),
                -1.5,
                1.5,
            )
            # A valid A-share behavioral target already includes inverse-risk
            # sizing and a 35% single-name cap. Fundamentals may adjust the
            # expected return, but must not expand that portfolio target.
            if not use_behavioral:
                bl_target *= fundamental_anchor_multiplier
    target_pct = _clamp(bl_target / 100.0, 0.0, 1.0)
    regime = _regime_label(regime_brief)

    if price <= 0 or hand_cost <= 0:
        return OptimizedDecision(
            verdict="HOLD", alloc_cny=0, confidence=0.35, lots=0,
            hand_cost=hand_cost, edge_cny=0.0, expected_return_pct=0.0,
            sigma_30d_pct=0.0, p_directional=0.5, odds=0.0, kelly_fraction=0.0,
            target_position_pct=target_pct * 100.0,
            cvar_95_loss_pct=0.0,
            reason="missing_price_or_hand_cost",
            fundamental_score=fundamental_score,
            fundamental_model=fundamental_model,
            fundamental_anchor_multiplier=fundamental_anchor_multiplier,
            fundamental_return_adjustment_pct=fundamental_return_adj,
            exit_policy_adjustment_pct=0.0,
            exit_policy_reliability=0.0,
            exit_policy_evidence_score=0.0,
        )

    if use_behavioral:
        # For A shares the validated cross-sectional factor replaces the old
        # momentum/RSI/regime fallback. Fundamentals remain a bounded overlay;
        # stop/take-profit evidence is applied separately below.
        mu_pct = _clamp(
            _safe_float(getattr(behavioral_assessment, "expected_return_pct", 0.0))
            + fundamental_return_adj,
            -15.0,
            15.0,
        )
    else:
        mu_pct = _estimate_expected_return_pct(
            parsed=parsed, metrics=metrics, regime=regime,
            regime_probability=regime_probability,
            conditional_return_stats=conditional_return_stats,
            fundamental_assessment=fundamental_assessment,
        )
    exit_policy_adj, exit_policy_reliability, exit_policy_evidence = _exit_policy_adjustment_pct(
        position_exit_policy=position_exit_policy,
        holding_value=holding_value,
        total=total,
    )
    mu_pct = _clamp(mu_pct - exit_policy_adj, -15.0, 15.0)
    sigma_pct = _sigma_30d_pct(metrics)
    mu = mu_pct / 100.0
    sigma = sigma_pct / 100.0

    if (
        conditional_return_stats is not None
        and not getattr(conditional_return_stats, "low_confidence", False)
    ):
        upside_pct = max(
            _safe_float(getattr(conditional_return_stats, "upside_mean_pct", 0.0)),
            _safe_float(getattr(conditional_return_stats, "q80_return_pct", 0.0)),
            2.0,
        )
        downside_pct = max(
            abs(_safe_float(getattr(conditional_return_stats, "downside_mean_pct", 0.0))),
            _safe_float(getattr(conditional_return_stats, "cvar_95_loss_pct", 0.0)),
            abs(_safe_float(getattr(conditional_return_stats, "q20_return_pct", 0.0))),
            2.0,
        )
    else:
        upside_pct = max(abs(mu_pct), 1.5 * _safe_float(metrics.get("atr_pct"), 2.0), 2.0)
        downside_pct = max(2.0 * _safe_float(metrics.get("atr_pct"), 2.0), sigma_pct * 0.55, 2.0)
    odds = max(upside_pct / downside_pct, 0.01)
    p_buy = _directional_probability(
        mu_pct=mu_pct, sigma_pct=sigma_pct,
        regime_probability=regime_probability,
        conditional_return_stats=conditional_return_stats,
        for_sell=False,
    )
    kelly = p_buy - (1.0 - p_buy) / odds
    fractional_kelly = max(0.0, kelly) * 0.25

    max_buy_by_cash = int(cash // hand_cost)
    max_buy_by_total_assets = int(max(0.0, total - holding_value) // hand_cost)
    max_buy_lots = min(max_buy_by_cash, max_buy_by_total_assets)
    max_sell_lots = int(holding_value // hand_cost)

    fee_rate = 0.0020 if market.lower() == "hk" else 0.0013
    risk_lambda = {
        "conservative": 1.2,
        "moderate": 0.8,
        "balanced": 0.8,
        "aggressive": 0.5,
    }.get((risk_preference or "moderate").lower(), 0.8)
    # Black-Litterman anchor: target_position_pct is the strategic posterior
    # weight. It is a soft penalty, so strong regime-conditioned edge can still
    # justify moving away from the anchor.
    if use_behavioral:
        configured_weight = _safe_float(
            getattr(behavioral_assessment, "optimizer_weight", 0.0), 0.0,
        )
        if configured_weight > 0:
            anchor_lambda = _clamp(configured_weight, 0.55, 1.0)
        else:
            # Compatibility for older callers that have not supplied the
            # per-symbol trailing-three-month factor assessment yet.
            factor_sample_size = max(
                0.0,
                _safe_float(getattr(behavioral_assessment, "sample_size", 0.0), 0.0),
            )
            factor_reliability = factor_sample_size / (factor_sample_size + 20.0)
            anchor_lambda = 0.55 + 0.45 * factor_reliability
    else:
        anchor_lambda = 0.25
    kelly_lambda = {
        "conservative": 0.35,
        "moderate": 0.20,
        "balanced": 0.20,
        "aggressive": 0.10,
    }.get((risk_preference or "moderate").lower(), 0.20)
    cvar_loss_pct = (
        _safe_float(getattr(conditional_return_stats, "cvar_95_loss_pct", 0.0))
        if conditional_return_stats is not None
        else 0.0
    )
    tail_sigma = max(sigma, cvar_loss_pct / 100.0)

    def utility(delta_cny: float) -> float:
        new_holding = _clamp(holding_value + delta_cny, 0.0, total)
        expected = delta_cny * mu
        cost = abs(delta_cny) * fee_rate
        risk_change = 0.5 * risk_lambda * ((new_holding ** 2 - holding_value ** 2) / total) * (tail_sigma ** 2)
        current_conc = holding_value / total
        new_conc = new_holding / total
        conc_change = max(0.0, new_conc - 0.60) ** 2 - max(0.0, current_conc - 0.60) ** 2
        concentration_penalty = conc_change * total * 0.35
        anchor_change = (new_conc - target_pct) ** 2 - (current_conc - target_pct) ** 2
        anchor_penalty = anchor_change * total * anchor_lambda
        kelly_budget = max(fractional_kelly, 0.0)
        kelly_change = (
            max(0.0, new_conc - kelly_budget) ** 2
            - max(0.0, current_conc - kelly_budget) ** 2
        )
        kelly_penalty = kelly_change * total * kelly_lambda
        return expected - cost - risk_change - concentration_penalty - anchor_penalty - kelly_penalty

    candidates: list[tuple[str, int, float]] = [("HOLD", 0, 0.0)]
    for lots in _candidate_lots(max_buy_lots):
        delta = lots * hand_cost
        verdict = "BUY" if delta >= max(cash * 0.50, total * 0.08) else "ACCUMULATE"
        candidates.append((verdict, lots, delta))
    for lots in _candidate_lots(max_sell_lots):
        delta = -lots * hand_cost
        verdict = "SELL" if lots >= max_sell_lots else "TRIM"
        candidates.append((verdict, -lots, delta))

    best_verdict, best_lots, best_delta = max(candidates, key=lambda c: utility(c[2]))
    edge = utility(best_delta)
    min_edge = max(hand_cost * 0.003, total * 0.0002, 20.0)
    if edge <= min_edge:
        best_verdict, best_lots, best_delta, edge = "HOLD", 0, 0.0, 0.0

    # The production portfolio refreshes its factor target every five sessions.
    # target is stable between refreshes; this five-percentage-point no-trade
    # band prevents ten-minute committee runs from churning around that target.
    # Reliable position-risk sell evidence is deliberately allowed through.
    if use_behavioral and abs(_safe_float(position_pct) - target_pct * 100.0) <= 5.0:
        risk_sell_bypass = (
            best_verdict in {"TRIM", "SELL"}
            and exit_policy_reliability >= 0.50
            and exit_policy_adj >= 1.0
        )
        if not risk_sell_bypass:
            best_verdict, best_lots, best_delta, edge = "HOLD", 0, 0.0, 0.0

    alloc = int(round(best_delta))
    if best_verdict in {"BUY", "ACCUMULATE"} and alloc <= 0:
        best_verdict, best_lots, alloc, edge = "HOLD", 0, 0, 0.0
    if best_verdict in {"TRIM", "SELL"} and alloc >= 0:
        best_verdict, best_lots, alloc, edge = "HOLD", 0, 0, 0.0

    p_directional = _directional_probability(
        mu_pct=mu_pct, sigma_pct=sigma_pct,
        regime_probability=regime_probability,
        conditional_return_stats=conditional_return_stats,
        for_sell=best_verdict in {"TRIM", "SELL"},
    )
    if best_verdict == "HOLD":
        confidence = min(_safe_float(parsed.get("confidence"), 0.5), 0.55)
        reason = "best_edge_not_above_hold_after_costs_or_constraints"
    else:
        edge_score = _sigmoid(edge / max(hand_cost * 0.01, 100.0))
        confidence = _clamp(0.65 * p_directional + 0.35 * edge_score, 0.35, 0.92)
        reason = "max_expected_utility_discrete_lot_action"

    return OptimizedDecision(
        verdict=best_verdict,
        alloc_cny=alloc,
        confidence=round(confidence, 4),
        lots=abs(best_lots),
        hand_cost=hand_cost,
        edge_cny=edge,
        expected_return_pct=mu_pct,
        sigma_30d_pct=sigma_pct,
        p_directional=p_directional,
        odds=odds,
        kelly_fraction=fractional_kelly,
        target_position_pct=target_pct * 100.0,
        cvar_95_loss_pct=cvar_loss_pct,
        reason=reason,
        fundamental_score=fundamental_score,
        fundamental_model=fundamental_model,
        fundamental_anchor_multiplier=fundamental_anchor_multiplier,
        fundamental_return_adjustment_pct=fundamental_return_adj,
        exit_policy_adjustment_pct=exit_policy_adj,
        exit_policy_reliability=exit_policy_reliability,
        exit_policy_evidence_score=exit_policy_evidence,
        behavioral_factor_score=_safe_float(getattr(behavioral_assessment, "score", 0.0), 0.0),
        behavioral_target_weight_pct=_safe_float(getattr(behavioral_assessment, "target_weight_pct", 0.0), 0.0),
        behavioral_selected=bool(getattr(behavioral_assessment, "selected", False)),
        behavioral_low_confidence=bool(getattr(behavioral_assessment, "low_confidence", True)),
        behavioral_model=str(getattr(behavioral_assessment, "model_key", "none")),
        behavioral_optimizer_weight=anchor_lambda if use_behavioral else 0.0,
        behavioral_trailing_3m_return_pct=_safe_float(
            getattr(behavioral_assessment, "trailing_3m_factor_return_pct", 0.0), 0.0,
        ),
        behavioral_trailing_3m_hit_rate=_safe_float(
            getattr(behavioral_assessment, "trailing_3m_hit_rate", 0.5), 0.5,
        ),
        behavioral_trailing_3m_sample_size=int(
            _safe_float(getattr(behavioral_assessment, "trailing_3m_sample_size", 0), 0.0)
        ),
    )
