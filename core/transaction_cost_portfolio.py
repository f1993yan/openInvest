"""Long-only portfolio allocation with explicit cash and turnover costs.

Hautsch and Voigt (2019), doi:10.1016/j.jeconom.2019.04.028.
This applies their L1/L2 turnover regularization to the existing A-share alpha;
it does not reproduce their forecasting model. Parameters were frozen using
the pre-evaluation sample, not claimed to be universally optimal.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

MODEL_KEY = "ashare_transaction_cost_regularized_v1"


@dataclass(frozen=True)
class AllocationParameters:
    risk_aversion: float = 2.0
    l2_turnover: float = 0.02
    covariance_sessions: int = 126
    diagonal_shrinkage: float = 0.20
    horizon_sessions: int = 5
    alpha_horizon_sessions: int = 20
    fee_rate: float = 0.0013
    slippage_rate: float = 0.0005
    minimum_fee_cny: float = 5.0
    max_name_weight: float = 0.35
    max_gross_weight: float = 0.95


PARAMETERS = AllocationParameters()


def portfolio_decision_as_optimizer(decision: dict, *, price: float, factor: Any = None):
    """Keep the existing response/synthesis contract without a second optimizer."""
    from core.decision_optimizer import OptimizedDecision

    return OptimizedDecision(
        verdict=str(decision.get("verdict") or "WAIT"),
        alloc_cny=float(decision.get("alloc_cny", 0)), confidence=0.5,
        lots=int(float(decision.get("units", 0))//100), hand_cost=price*100,
        edge_cny=0, expected_return_pct=float(getattr(factor, "expected_return_pct", 0)),
        sigma_30d_pct=0, p_directional=0.5, odds=0, kelly_fraction=0,
        target_position_pct=float(decision.get("target_weight_pct", 0)), cvar_95_loss_pct=0,
        reason=str(decision.get("reason") or "portfolio_plan_unavailable"),
        behavioral_factor_score=float(getattr(factor, "score", 0)),
        behavioral_target_weight_pct=float(decision.get("target_weight_pct", 0)),
        behavioral_selected=bool(decision.get("selected", False)),
        behavioral_low_confidence=bool(getattr(factor, "low_confidence", True)),
        behavioral_model=str(getattr(factor, "model_key", "none")),
        behavioral_optimizer_weight=0,
        behavioral_trailing_3m_return_pct=float(getattr(factor, "trailing_3m_factor_return_pct", 0)),
        behavioral_trailing_3m_hit_rate=float(getattr(factor, "trailing_3m_hit_rate", 0.5)),
        behavioral_trailing_3m_sample_size=int(getattr(factor, "trailing_3m_sample_size", 0)))


def historical_covariance(
    histories: Mapping[str, pd.DataFrame], symbols: list[str], *, as_of: str,
    parameters: AllocationParameters = PARAMETERS,
) -> np.ndarray:
    """Use completed bars only; require aligned, sufficient observations."""
    closes = {}
    for symbol in symbols:
        frame = histories[symbol].copy()
        frame.index = pd.to_datetime(frame.index).tz_localize(None)
        frame = frame.loc[frame.index <= pd.Timestamp(as_of)].sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        if "Close" not in frame or len(frame) < parameters.covariance_sessions+1:
            raise ValueError("insufficient_covariance_history")
        close = pd.to_numeric(frame.Close, errors="coerce")
        if close.isna().any() or (close <= 0).any():
            raise ValueError("invalid_covariance_prices")
        closes[symbol] = close
    prices = pd.DataFrame(closes).ffill()
    returns = prices.pct_change(fill_method=None).dropna().tail(parameters.covariance_sessions)
    if len(returns) < parameters.covariance_sessions:
        raise ValueError("insufficient_aligned_covariance_history")
    covariance = returns.cov().to_numpy() * parameters.horizon_sessions
    rho = parameters.diagonal_shrinkage
    covariance = (1-rho)*covariance + rho*np.diag(np.diag(covariance))
    covariance += np.eye(len(symbols))*1e-8
    if not np.isfinite(covariance).all():
        raise ValueError("nonfinite_covariance")
    return covariance


def optimize_transaction_cost_portfolio(
    *, symbols: list[str], current_weights: Mapping[str, float],
    selected: set[str], expected_returns_pct: Mapping[str, float],
    covariance: np.ndarray, budget_fraction: float = 1.0,
    fixed_weight: float = 0.0, cash_reserve: float = 0.05,
    parameters: AllocationParameters = PARAMETERS,
) -> dict[str, Any]:
    """Solve the same one-period L1+L2 objective as the isolated research."""
    import cvxpy as cp

    n = len(symbols)
    if not n or budget_fraction <= 0:
        return {"model": MODEL_KEY, "weights": {s: 0.0 for s in symbols},
                "status": "cash", "parameters": asdict(parameters)}
    current = np.array([current_weights.get(s, 0.0) for s in symbols], dtype=float)
    upper = np.array([parameters.max_name_weight if s in selected else 0.0 for s in symbols])
    mu = np.array([np.clip(expected_returns_pct.get(s, 0.0), -15, 15)/100
                   * parameters.horizon_sessions/parameters.alpha_horizon_sessions for s in symbols])
    if covariance.shape != (n, n) or not all(np.isfinite(a).all() for a in [current, covariance, mu]):
        raise ValueError("invalid_portfolio_inputs")
    if (current < 0).any() or not 0 < budget_fraction <= 1.000001:
        raise ValueError("invalid_portfolio_budget")
    w, cash, cost = cp.Variable(n), cp.Variable(), cp.Variable(nonneg=True)
    cost_rate = parameters.fee_rate+parameters.slippage_rate
    # Foreign positions are fixed. Reserve all attainable cash when they leave
    # less than the requested reserve, including the cost of liquidating A shares.
    # Requiring B*(1-cost) in that case would make even a full exit infeasible.
    exit_cost = cost_rate*float(current.sum())
    attainable_cash = max(0, (budget_fraction-exit_cost)/max(1-exit_cost, 1e-12))
    floor = min(max(cash_reserve, 0), attainable_cash)
    constraints = [w >= 0, w <= cp.multiply(upper, 1-cost),
                   cp.sum(w)+cash == budget_fraction-cost,
                   cash >= floor*(1-cost),
                   cp.sum(w) <= max(0, parameters.max_gross_weight-fixed_weight)*(1-cost),
                   cost >= cost_rate*cp.norm1(w-current)]
    objective = (mu@w - parameters.risk_aversion/2*cp.quad_form(w, cp.psd_wrap(covariance))
                 - cost - parameters.l2_turnover/2*cp.sum_squares(w-current))
    problem = cp.Problem(cp.Maximize(objective), constraints)
    problem.solve(solver="CLARABEL", max_iter=300, tol_feas=1e-9)
    if problem.status not in {cp.OPTIMAL, cp.OPTIMAL_INACCURATE} or w.value is None:
        raise RuntimeError("portfolio_solver_unavailable")
    violation = max(float(np.max(np.abs(c.violation()))) for c in constraints)
    if violation > 1e-6:
        raise RuntimeError("portfolio_solver_infeasible_residual")
    weights = np.clip(np.asarray(w.value).flatten(), 0, upper)
    weights[upper == 0] = 0.0
    return {"model": MODEL_KEY, "weights": dict(zip(symbols, weights.tolist())),
            "cash_weight": float(cash.value), "estimated_cost_weight": float(cost.value),
            "cash_reserve_weight_floor": floor,
            "objective": float(problem.value), "status": problem.status,
            "max_constraint_violation": violation, "parameters": asdict(parameters)}
