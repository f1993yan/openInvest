"""Trading mode normalization and deterministic alert-selection modifiers."""
from __future__ import annotations

from typing import Any, Dict


TRADE_MODE_ACTIVE_PROFIT = "active_profit"
TRADE_MODE_CASH_RECOVERY = "cash_recovery"
TRADE_MODE_RISK_OFF = "risk_off"

DEFAULT_TRADING_MODE = TRADE_MODE_ACTIVE_PROFIT

TRADING_MODE_LABELS = {
    TRADE_MODE_ACTIVE_PROFIT: "主动盈利",
    TRADE_MODE_CASH_RECOVERY: "现金回收",
    TRADE_MODE_RISK_OFF: "主动避险",
}

_ALIASES = {
    "active_profit": TRADE_MODE_ACTIVE_PROFIT,
    "profit": TRADE_MODE_ACTIVE_PROFIT,
    "default": TRADE_MODE_ACTIVE_PROFIT,
    "主动盈利": TRADE_MODE_ACTIVE_PROFIT,
    "现金回收": TRADE_MODE_CASH_RECOVERY,
    "cash_recovery": TRADE_MODE_CASH_RECOVERY,
    "cash": TRADE_MODE_CASH_RECOVERY,
    "主动避险": TRADE_MODE_RISK_OFF,
    "risk_off": TRADE_MODE_RISK_OFF,
    "bear": TRADE_MODE_RISK_OFF,
    "defensive": TRADE_MODE_RISK_OFF,
}


def normalize_trading_mode(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return DEFAULT_TRADING_MODE
    return _ALIASES.get(text, _ALIASES.get(text.lower(), DEFAULT_TRADING_MODE))


def trading_mode_label(value: Any) -> str:
    mode = normalize_trading_mode(value)
    return TRADING_MODE_LABELS.get(mode, TRADING_MODE_LABELS[DEFAULT_TRADING_MODE])


def trading_mode_payload(value: Any) -> Dict[str, str]:
    mode = normalize_trading_mode(value)
    return {"mode": mode, "label": trading_mode_label(mode)}


def trading_mode_buy_threshold(value: Any) -> float:
    mode = normalize_trading_mode(value)
    if mode == TRADE_MODE_CASH_RECOVERY:
        return 63.0
    if mode == TRADE_MODE_RISK_OFF:
        return 68.0
    return 55.0


def trading_mode_cash_reserve_ratio(value: Any) -> float:
    mode = normalize_trading_mode(value)
    if mode == TRADE_MODE_CASH_RECOVERY:
        return 0.35
    if mode == TRADE_MODE_RISK_OFF:
        return 0.50
    return 0.0


def trading_mode_buy_value_adjustment(value: Any) -> float:
    mode = normalize_trading_mode(value)
    if mode == TRADE_MODE_CASH_RECOVERY:
        return -18.0
    if mode == TRADE_MODE_RISK_OFF:
        return -24.0
    return 0.0


def trading_mode_buy_lot_multiplier(value: Any) -> float:
    mode = normalize_trading_mode(value)
    if mode == TRADE_MODE_CASH_RECOVERY:
        return 0.5
    if mode == TRADE_MODE_RISK_OFF:
        return 0.33
    return 1.0


def trading_mode_sell_threshold_adjustment(value: Any, *, has_trigger: bool, policy_edge: float) -> float:
    mode = normalize_trading_mode(value)
    positive_policy = max(0.0, float(policy_edge or 0.0))
    if mode == TRADE_MODE_CASH_RECOVERY:
        return -(3.0 + 4.0 * positive_policy)
    if mode == TRADE_MODE_RISK_OFF:
        trigger_bonus = 3.0 if has_trigger else 0.0
        return -(2.0 + trigger_bonus + 5.0 * positive_policy)
    return 0.0


def trading_mode_sell_score_bonus(value: Any, *, release_cash_cny: float, portfolio_value: float) -> float:
    mode = normalize_trading_mode(value)
    if mode == TRADE_MODE_ACTIVE_PROFIT:
        return 0.0
    release_pct = max(0.0, float(release_cash_cny or 0.0)) / max(float(portfolio_value or 0.0), 1.0) * 100.0
    if mode == TRADE_MODE_CASH_RECOVERY:
        return min(12.0, 2.0 + release_pct * 1.2)
    if mode == TRADE_MODE_RISK_OFF:
        return min(10.0, 1.0 + release_pct * 0.9)
    return 0.0
