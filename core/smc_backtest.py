"""Smart Money Concepts (SMC) strategy backtester.

This module is intentionally deterministic and LLM-free.  It implements a
compact SMC rule set:

- swing highs/lows from local pivots;
- BOS / CHOCH from closes breaking the latest confirmed swing;
- bullish / bearish fair value gaps;
- liquidity sweeps of prior swing levels;
- order-block style entries after a break of structure.

The model is not a promise that SMC is profitable.  It is a repeatable research
tool for testing whether these concepts have edge on a given OHLCV series.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SMCSignal:
    date: str
    kind: str
    direction: str
    price: float
    level: float
    note: str = ""


@dataclass(frozen=True)
class SMCTrade:
    entry_date: str
    exit_date: str
    direction: str
    entry_price: float
    exit_price: float
    stop_price: float
    take_profit_price: float
    qty: float
    pnl: float
    return_pct: float
    exit_reason: str


@dataclass(frozen=True)
class SMCBacktestConfig:
    swing_lookback: int = 3
    atr_window: int = 14
    risk_per_trade_pct: float = 1.0
    initial_cash: float = 100_000.0
    reward_risk: float = 2.0
    max_hold_bars: int = 20
    min_stop_atr: float = 0.8
    require_fvg: bool = False
    require_liquidity_sweep: bool = False
    allow_short: bool = False
    fee_bps: float = 5.0


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Return sorted OHLCV data with required columns and numeric values."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    out = df.copy()
    rename = {c: c.capitalize() for c in out.columns if str(c).lower() in {"open", "high", "low", "close", "volume"}}
    out = out.rename(columns=rename)
    for col in ["Open", "High", "Low", "Close"]:
        if col not in out.columns:
            raise ValueError(f"missing OHLC column: {col}")
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    out["Volume"] = pd.to_numeric(out["Volume"], errors="coerce").fillna(0.0)
    out = out.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()
    return out


def add_smc_features(df: pd.DataFrame, config: SMCBacktestConfig | None = None) -> pd.DataFrame:
    """Annotate OHLCV data with SMC feature columns."""
    cfg = config or SMCBacktestConfig()
    out = normalize_ohlcv(df)
    if out.empty:
        return out

    high = out["High"]
    low = out["Low"]
    close = out["Close"]
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["ATR"] = tr.rolling(cfg.atr_window, min_periods=1).mean()

    n = cfg.swing_lookback
    out["swing_high"] = False
    out["swing_low"] = False
    for i in range(n, len(out) - n):
        window_high = high.iloc[i - n : i + n + 1]
        window_low = low.iloc[i - n : i + n + 1]
        out.iat[i, out.columns.get_loc("swing_high")] = high.iloc[i] == window_high.max()
        out.iat[i, out.columns.get_loc("swing_low")] = low.iloc[i] == window_low.min()

    out["last_swing_high"] = np.nan
    out["last_swing_low"] = np.nan
    last_high = np.nan
    last_low = np.nan
    for i in range(len(out)):
        out.iat[i, out.columns.get_loc("last_swing_high")] = last_high
        out.iat[i, out.columns.get_loc("last_swing_low")] = last_low
        if bool(out["swing_high"].iloc[i]):
            last_high = float(high.iloc[i])
        if bool(out["swing_low"].iloc[i]):
            last_low = float(low.iloc[i])

    out["bullish_bos"] = close > out["last_swing_high"]
    out["bearish_bos"] = close < out["last_swing_low"]
    out["bullish_choch"] = False
    out["bearish_choch"] = False
    structure = "neutral"
    for i in range(len(out)):
        bullish_break = bool(out["bullish_bos"].iloc[i])
        bearish_break = bool(out["bearish_bos"].iloc[i])
        if bullish_break and structure == "bearish":
            out.iat[i, out.columns.get_loc("bullish_choch")] = True
        if bearish_break and structure == "bullish":
            out.iat[i, out.columns.get_loc("bearish_choch")] = True
        if bullish_break:
            structure = "bullish"
        elif bearish_break:
            structure = "bearish"
    out["bullish_fvg"] = low > high.shift(2)
    out["bearish_fvg"] = high < low.shift(2)
    out["bullish_liquidity_sweep"] = (low < out["last_swing_low"]) & (close > out["last_swing_low"])
    out["bearish_liquidity_sweep"] = (high > out["last_swing_high"]) & (close < out["last_swing_high"])
    return out


def generate_smc_signals(df: pd.DataFrame, config: SMCBacktestConfig | None = None) -> List[SMCSignal]:
    """Generate human-readable SMC events from OHLCV data."""
    cfg = config or SMCBacktestConfig()
    data = add_smc_features(df, cfg)
    signals: List[SMCSignal] = []
    for idx, row in data.iterrows():
        date = _date_str(idx)
        if bool(row.get("bullish_bos")):
            signals.append(SMCSignal(date, "BOS", "long", float(row["Close"]), float(row["last_swing_high"]), "close broke prior swing high"))
        if bool(row.get("bearish_bos")):
            signals.append(SMCSignal(date, "BOS", "short", float(row["Close"]), float(row["last_swing_low"]), "close broke prior swing low"))
        if bool(row.get("bullish_choch")):
            signals.append(SMCSignal(date, "CHOCH", "long", float(row["Close"]), float(row["last_swing_high"]), "bullish change of character"))
        if bool(row.get("bearish_choch")):
            signals.append(SMCSignal(date, "CHOCH", "short", float(row["Close"]), float(row["last_swing_low"]), "bearish change of character"))
        if bool(row.get("bullish_fvg")):
            signals.append(SMCSignal(date, "FVG", "long", float(row["Close"]), float(row["High"]), "bullish imbalance"))
        if bool(row.get("bearish_fvg")):
            signals.append(SMCSignal(date, "FVG", "short", float(row["Close"]), float(row["Low"]), "bearish imbalance"))
        if bool(row.get("bullish_liquidity_sweep")):
            signals.append(SMCSignal(date, "SWEEP", "long", float(row["Close"]), float(row["last_swing_low"]), "swept sell-side liquidity and closed back above"))
        if bool(row.get("bearish_liquidity_sweep")):
            signals.append(SMCSignal(date, "SWEEP", "short", float(row["Close"]), float(row["last_swing_high"]), "swept buy-side liquidity and closed back below"))
    return signals


def backtest_smc_strategy(df: pd.DataFrame, config: SMCBacktestConfig | None = None) -> Dict[str, Any]:
    """Run a single-position SMC backtest and return metrics plus trades."""
    cfg = config or SMCBacktestConfig()
    data = add_smc_features(df, cfg)
    if len(data) < max(cfg.swing_lookback * 2 + 5, cfg.atr_window):
        return _empty_result(cfg)

    cash = float(cfg.initial_cash)
    equity_curve: List[tuple[str, float]] = []
    trades: List[SMCTrade] = []
    position: Optional[Dict[str, Any]] = None
    fee_rate = cfg.fee_bps / 10_000
    trend = "neutral"

    for i, (idx, row) in enumerate(data.iterrows()):
        date = _date_str(idx)
        open_price = float(row["Open"])
        high = float(row["High"])
        low = float(row["Low"])
        close = float(row["Close"])

        if position is not None:
            exit_price, reason = _maybe_exit(position, high, low, close, i, cfg)
            if exit_price is not None:
                trade = _close_trade(position, date, exit_price, reason, fee_rate)
                trades.append(trade)
                cash += position["notional"] + trade.pnl
                position = None

        if position is None:
            entry_direction = _entry_direction(row, trend, cfg)
            if entry_direction:
                position = _open_position(entry_direction, date, i, open_price, row, cash, cfg, fee_rate)
                if position is not None:
                    cash -= position["notional"]

        if bool(row.get("bullish_bos")):
            trend = "bullish"
        elif bool(row.get("bearish_bos")):
            trend = "bearish"

        mark_value = cash
        if position is not None:
            mark_value += position["notional"] + _floating_pnl(position, close)
        equity_curve.append((date, round(mark_value, 2)))

    if position is not None:
        idx = data.index[-1]
        close = float(data["Close"].iloc[-1])
        trade = _close_trade(position, _date_str(idx), close, "end_of_data", fee_rate)
        trades.append(trade)
        cash += position["notional"] + trade.pnl
        equity_curve[-1] = (_date_str(idx), round(cash, 2))

    metrics = _metrics(equity_curve, trades, cfg.initial_cash)
    return {
        "config": asdict(cfg),
        "metrics": metrics,
        "trades": [asdict(t) for t in trades],
        "equity_curve": equity_curve,
        "signals": [asdict(s) for s in generate_smc_signals(data, cfg)],
    }


def _entry_direction(row: pd.Series, trend: str, cfg: SMCBacktestConfig) -> Optional[str]:
    bullish_ok = bool(row.get("bullish_bos")) or bool(row.get("bullish_choch"))
    bearish_ok = bool(row.get("bearish_bos")) or bool(row.get("bearish_choch"))
    if cfg.require_fvg:
        bullish_ok = bullish_ok and bool(row.get("bullish_fvg"))
        bearish_ok = bearish_ok and bool(row.get("bearish_fvg"))
    if cfg.require_liquidity_sweep:
        bullish_ok = bullish_ok and bool(row.get("bullish_liquidity_sweep"))
        bearish_ok = bearish_ok and bool(row.get("bearish_liquidity_sweep"))
    if bullish_ok:
        return "long"
    if bearish_ok and cfg.allow_short:
        return "short"
    return None


def _open_position(direction: str, date: str, bar_index: int, entry: float, row: pd.Series, cash: float, cfg: SMCBacktestConfig, fee_rate: float) -> Optional[Dict[str, Any]]:
    atr = max(float(row.get("ATR") or 0.0), 1e-9)
    if direction == "long":
        structural_stop = float(row.get("last_swing_low") or np.nan)
        raw_stop = structural_stop if np.isfinite(structural_stop) else entry - atr
        stop = min(raw_stop, entry - cfg.min_stop_atr * atr)
        risk_per_unit = entry - stop
        take_profit = entry + cfg.reward_risk * risk_per_unit
    else:
        structural_stop = float(row.get("last_swing_high") or np.nan)
        raw_stop = structural_stop if np.isfinite(structural_stop) else entry + atr
        stop = max(raw_stop, entry + cfg.min_stop_atr * atr)
        risk_per_unit = stop - entry
        take_profit = entry - cfg.reward_risk * risk_per_unit
    if risk_per_unit <= 0:
        return None
    risk_budget = cash * cfg.risk_per_trade_pct / 100
    qty = risk_budget / risk_per_unit
    notional = qty * entry * (1 + fee_rate)
    if notional > cash:
        scale = cash / notional
        qty *= scale
        notional = cash
    if qty <= 0 or notional <= 0:
        return None
    return {
        "direction": direction,
        "entry_date": date,
        "entry_bar": bar_index,
        "entry": entry,
        "stop": stop,
        "take_profit": take_profit,
        "qty": qty,
        "notional": notional,
    }


def _maybe_exit(position: Dict[str, Any], high: float, low: float, close: float, bar_index: int, cfg: SMCBacktestConfig) -> tuple[Optional[float], str]:
    if position["direction"] == "long":
        if low <= position["stop"]:
            return float(position["stop"]), "stop_loss"
        if high >= position["take_profit"]:
            return float(position["take_profit"]), "take_profit"
    else:
        if high >= position["stop"]:
            return float(position["stop"]), "stop_loss"
        if low <= position["take_profit"]:
            return float(position["take_profit"]), "take_profit"
    if bar_index - int(position["entry_bar"]) >= cfg.max_hold_bars:
        return close, "time_exit"
    return None, ""


def _close_trade(position: Dict[str, Any], exit_date: str, exit_price: float, reason: str, fee_rate: float) -> SMCTrade:
    pnl = _floating_pnl(position, exit_price)
    fee = abs(exit_price * position["qty"]) * fee_rate
    pnl -= fee
    ret = pnl / max(position["notional"], 1e-9) * 100
    return SMCTrade(
        entry_date=position["entry_date"],
        exit_date=exit_date,
        direction=position["direction"],
        entry_price=round(position["entry"], 4),
        exit_price=round(exit_price, 4),
        stop_price=round(position["stop"], 4),
        take_profit_price=round(position["take_profit"], 4),
        qty=round(position["qty"], 4),
        pnl=round(pnl, 2),
        return_pct=round(ret, 4),
        exit_reason=reason,
    )


def _floating_pnl(position: Dict[str, Any], price: float) -> float:
    if position["direction"] == "long":
        return (price - position["entry"]) * position["qty"]
    return (position["entry"] - price) * position["qty"]


def _metrics(equity_curve: List[tuple[str, float]], trades: List[SMCTrade], initial_cash: float) -> Dict[str, Any]:
    final_equity = equity_curve[-1][1] if equity_curve else initial_cash
    total_return = (final_equity / initial_cash - 1) * 100 if initial_cash > 0 else 0.0
    values = np.array([v for _, v in equity_curve], dtype=float) if equity_curve else np.array([initial_cash])
    peak = np.maximum.accumulate(values)
    dd = (values - peak) / np.maximum(peak, 1e-9)
    returns = np.diff(values) / np.maximum(values[:-1], 1e-9) if len(values) > 1 else np.array([])
    sharpe = 0.0
    if len(returns) > 1 and np.std(returns, ddof=1) > 1e-9:
        sharpe = float(np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(252))
    wins = [t for t in trades if t.pnl > 0]
    gross_win = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
    return {
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return, 4),
        "max_drawdown_pct": round(abs(float(dd.min())) * 100, 4),
        "sharpe_ratio": round(sharpe, 4),
        "trade_count": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0),
    }


def _empty_result(cfg: SMCBacktestConfig) -> Dict[str, Any]:
    return {"config": asdict(cfg), "metrics": _metrics([], [], cfg.initial_cash), "trades": [], "equity_curve": [], "signals": []}


def _date_str(idx: Any) -> str:
    try:
        return pd.to_datetime(idx).strftime("%Y-%m-%d")
    except Exception:
        return str(idx)


__all__ = [
    "SMCBacktestConfig",
    "SMCSignal",
    "SMCTrade",
    "add_smc_features",
    "backtest_smc_strategy",
    "generate_smc_signals",
    "normalize_ohlcv",
]
