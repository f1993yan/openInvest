"""Deterministic entry/exit point calculator.

The calculator turns the same inputs used by the execution optimizer into
actionable price levels. It uses three model families that are auditable and
data-driven:

- Regime-conditioned empirical forward-return quantiles for upside/downside.
- Volatility-scaled levels via ATR percent.
- CVaR tail-loss summaries for stop distance under adverse regimes.

Recent references for these ingredients:
- Regime/CVaR portfolio risk: Dai, Z. et al. (2021), "Robust portfolio
  selection with regime switching and asymmetric dependence".
  https://doi.org/10.1016/j.econmod.2021.03.011
- Dynamic allocation under time-varying risk: Calafiore, G. C. et al. (2021),
  "Time-Varying Risk Aversion and Dynamic Portfolio Allocation".
  https://doi.org/10.1287/opre.2020.2095
- Modern volatility targeting / risk scaling background: Moreira, A. and
  Muir, T. (2017), "Volatility-Managed Portfolios".
  https://doi.org/10.1111/jofi.12513
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class EntryExitPlan:
    symbol: str
    model_name: str
    current_price: float
    buy_pullback_price: float
    buy_breakout_price: float
    stop_loss_price: float
    take_profit_price: float
    trim_price: float
    reentry_price: float
    expected_return_pct: float
    downside_quantile_pct: float
    upside_quantile_pct: float
    cvar_95_loss_pct: float
    ma20: float
    ma120: float
    atr_pct: float
    reward_risk_ratio: float
    low_confidence: bool
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def audit_text(self) -> str:
        return (
            "\n\n[ENTRY_EXIT_POINTS]\n"
            f"model={self.model_name} low_confidence={self.low_confidence}\n"
            f"current_price: {self.current_price:.2f}\n"
            f"buy_pullback_price: {self.buy_pullback_price:.2f}\n"
            f"buy_breakout_price: {self.buy_breakout_price:.2f}\n"
            f"stop_loss_price: {self.stop_loss_price:.2f}\n"
            f"take_profit_price: {self.take_profit_price:.2f}\n"
            f"trim_price: {self.trim_price:.2f}\n"
            f"reentry_price: {self.reentry_price:.2f}\n"
            f"expected_return_30d: {self.expected_return_pct:+.2f}% "
            f"q20={self.downside_quantile_pct:+.2f}% q80={self.upside_quantile_pct:+.2f}% "
            f"cvar95_loss={self.cvar_95_loss_pct:.2f}% atr={self.atr_pct:.2f}% "
            f"reward_risk={self.reward_risk_ratio:.2f}\n"
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


def _round_price(price: float, market: str) -> float:
    # This keeps price output executable enough for A/HK stocks. Broker-side
    # tick ladders are more detailed for some HK price bands, but 0.01 is a
    # conservative display/alert granularity.
    tick = 0.01
    return round(max(price, tick) / tick) * tick


def _regime_label(regime_brief: str) -> str:
    for line in (regime_brief or "").splitlines():
        if line.startswith("REGIME:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def _fallback_expected_return_pct(metrics: Dict[str, Any], regime: str) -> float:
    regime_base = {
        "recovery": 3.0,
        "uptrend": 2.0,
        "range_bound": 0.0,
        "downtrend": -2.5,
        "crash": -5.0,
        "unknown": 0.0,
    }.get(regime, 0.0)
    ret30 = _safe_float(metrics.get("return_30d"), 0.0) * 100.0
    return _clamp(regime_base + _clamp(ret30 * 0.15, -2.0, 2.0), -15.0, 15.0)


def compute_entry_exit_points(
    *,
    symbol: str,
    current_price: Optional[float],
    metrics: Dict[str, Any],
    regime_brief: str = "",
    market: str = "a",
    conditional_return_stats: Optional[Any] = None,
    expected_return_pct: Optional[float] = None,
) -> EntryExitPlan:
    price = _safe_float(current_price or metrics.get("current_price"), 0.0)
    regime = _regime_label(regime_brief)
    atr_pct = max(_safe_float(metrics.get("atr_pct"), 2.0), 0.5)
    low_confidence = True

    if price <= 0:
        return EntryExitPlan(
            symbol=symbol,
            model_name="unavailable",
            current_price=0.0,
            buy_pullback_price=0.0,
            buy_breakout_price=0.0,
            stop_loss_price=0.0,
            take_profit_price=0.0,
            trim_price=0.0,
            reentry_price=0.0,
            expected_return_pct=0.0,
            downside_quantile_pct=0.0,
            upside_quantile_pct=0.0,
            cvar_95_loss_pct=0.0,
            ma20=_safe_float(metrics.get("ma20")),
            ma120=_safe_float(metrics.get("ma120")),
            atr_pct=atr_pct,
            reward_risk_ratio=0.0,
            low_confidence=True,
            reason="missing_current_price",
        )

    if conditional_return_stats is not None and not getattr(conditional_return_stats, "low_confidence", False):
        mu_pct = _safe_float(
            expected_return_pct,
            _safe_float(getattr(conditional_return_stats, "mean_return_pct", 0.0)),
        )
        q20 = _safe_float(getattr(conditional_return_stats, "q20_return_pct", -atr_pct))
        q80 = _safe_float(getattr(conditional_return_stats, "q80_return_pct", atr_pct))
        cvar_loss = max(_safe_float(getattr(conditional_return_stats, "cvar_95_loss_pct", 0.0)), 0.0)
        low_confidence = False
        model_name = "regime_quantile_atr_cvar"
    else:
        mu_pct = _safe_float(expected_return_pct, _fallback_expected_return_pct(metrics, regime))
        q20 = min(-1.0 * atr_pct, mu_pct - 1.25 * atr_pct)
        q80 = max(1.5 * atr_pct, mu_pct + 1.25 * atr_pct)
        cvar_loss = max(2.5 * atr_pct, abs(q20))
        model_name = "atr_regime_fallback"

    downside_move_pct = max(abs(min(q20, 0.0)), atr_pct)
    upside_move_pct = max(q80, 1.5 * atr_pct, 1.0)
    stop_loss_pct = max(2.0 * atr_pct, 0.55 * cvar_loss, downside_move_pct)
    take_profit_pct = max(upside_move_pct, 1.5 * stop_loss_pct)
    trim_pct = max(upside_move_pct, 2.0 * atr_pct)
    breakout_pct = max(0.5 * atr_pct, 0.6)
    pullback_pct = max(min(downside_move_pct, 2.0 * atr_pct), 0.6)

    buy_pullback = price * (1.0 - pullback_pct / 100.0)
    buy_breakout = price * (1.0 + breakout_pct / 100.0)
    stop_loss = price * (1.0 - stop_loss_pct / 100.0)
    take_profit = price * (1.0 + take_profit_pct / 100.0)
    trim_price = price * (1.0 + trim_pct / 100.0)
    reentry = price * (1.0 - downside_move_pct / 100.0)

    reward = take_profit - price
    risk = max(price - stop_loss, 0.01)
    reward_risk = reward / risk

    return EntryExitPlan(
        symbol=symbol.upper(),
        model_name=model_name,
        current_price=_round_price(price, market),
        buy_pullback_price=_round_price(buy_pullback, market),
        buy_breakout_price=_round_price(buy_breakout, market),
        stop_loss_price=_round_price(stop_loss, market),
        take_profit_price=_round_price(take_profit, market),
        trim_price=_round_price(trim_price, market),
        reentry_price=_round_price(reentry, market),
        expected_return_pct=round(mu_pct, 4),
        downside_quantile_pct=round(q20, 4),
        upside_quantile_pct=round(q80, 4),
        cvar_95_loss_pct=round(cvar_loss, 4),
        ma20=round(_safe_float(metrics.get("ma20")), 2),
        ma120=round(_safe_float(metrics.get("ma120")), 2),
        atr_pct=round(atr_pct, 4),
        reward_risk_ratio=round(reward_risk, 4),
        low_confidence=low_confidence,
        reason="conditional_quantile_volatility_scaled_levels",
    )


__all__ = ["EntryExitPlan", "compute_entry_exit_points"]
