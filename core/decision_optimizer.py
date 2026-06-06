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


def _sigma_30d_pct(metrics: Dict[str, Any]) -> float:
    vol_ann = metrics.get("volatility_annualized")
    if vol_ann is not None:
        return max(2.0, _safe_float(vol_ann) * math.sqrt(30.0 / 252.0) * 100.0)
    atr_pct = _safe_float(metrics.get("atr_pct"), 2.0)
    return max(2.0, atr_pct * math.sqrt(30.0 / 14.0))


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
    raw_target = bl_anchor_target_pct if bl_anchor_target_pct is not None else target_position_pct
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
        )

    mu_pct = _estimate_expected_return_pct(
        parsed=parsed, metrics=metrics, regime=regime,
        regime_probability=regime_probability,
        conditional_return_stats=conditional_return_stats,
        fundamental_assessment=fundamental_assessment,
    )
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
    )
