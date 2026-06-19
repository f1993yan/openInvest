"""A-share committee-style exit model backtest.

This research runner keeps the decision path deterministic and leak-free:
each trading-day signal only sees bars up to the previous close, while the next
bar's OHLC is used for executable A-share orders. It is designed for tuning
the post-entry stop/take-profit discipline used by the monitor window.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


DEFAULT_SYMBOLS: Dict[str, str] = {}


ENV_PARAM_KEYS = {
    "max_loss_pct": "INVEST_A_SHARE_POSITION_MAX_LOSS_PCT",
    "stop_atr_mult": "INVEST_A_SHARE_POSITION_STOP_ATR_MULT",
    "take_profit_r1": "INVEST_A_SHARE_POSITION_TAKE_PROFIT_R1",
    "take_profit_r2": "INVEST_A_SHARE_POSITION_TAKE_PROFIT_R2",
    "trailing_atr_mult": "INVEST_A_SHARE_POSITION_TRAIL_ATR_MULT",
}

SECTOR_POLICY_ENV_KEY = "INVEST_A_SHARE_SECTOR_EXIT_POLICIES"


@dataclass(frozen=True)
class ExitParams:
    max_loss_pct: float
    stop_atr_mult: float
    take_profit_r1: float
    take_profit_r2: float
    trailing_atr_mult: float
    min_score_to_buy: float


@dataclass
class Position:
    symbol: str
    name: str
    shares: int
    buy_date: str
    avg_cost: float
    hard_stop: float
    effective_stop: float
    take_profit_1: float
    take_profit_2: float
    highest_close: float
    atr_pct: float
    r1_taken: bool = False


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        return default if math.isnan(out) else out
    except (TypeError, ValueError):
        return default


def _round_price(value: float) -> float:
    return round(max(float(value), 0.0), 2)


def _a_share_limit_pct(symbol: str) -> float:
    if symbol.startswith(("300", "301", "688", "689")):
        return 20.0
    if symbol.startswith(("8", "4")):
        return 30.0
    return 10.0


def _lot_size(symbol: str, position: Optional[Position] = None) -> int:
    if symbol.startswith("688") and position is None:
        return 200
    return 100


def _is_limit_up(symbol: str, row: pd.Series, tolerance: float = 0.15) -> bool:
    prev = _safe_float(row.get("PrevClose"))
    close = _safe_float(row.get("Close"))
    high = _safe_float(row.get("High"))
    if prev <= 0:
        return False
    threshold = prev * (1.0 + (_a_share_limit_pct(symbol) - tolerance) / 100.0)
    return close >= threshold and high >= threshold


def _is_limit_down(symbol: str, row: pd.Series, tolerance: float = 0.15) -> bool:
    prev = _safe_float(row.get("PrevClose"))
    close = _safe_float(row.get("Close"))
    low = _safe_float(row.get("Low"))
    if prev <= 0:
        return False
    threshold = prev * (1.0 - (_a_share_limit_pct(symbol) - tolerance) / 100.0)
    return close <= threshold and low <= threshold


def _commission(value: float, fee_rate: float) -> float:
    return max(0.0, value * fee_rate)


def _normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.index = pd.to_datetime(out.index)
    out = out.sort_index()
    for col in ("Open", "High", "Low", "Close"):
        if col not in out.columns:
            if col == "Open" and "Close" in out.columns:
                out[col] = out["Close"]
            else:
                return pd.DataFrame()
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    out["Volume"] = pd.to_numeric(out["Volume"], errors="coerce").fillna(0.0)
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out["PrevClose"] = out["Close"].shift(1)
    return out.dropna(subset=["PrevClose"])


def _fetch_histories(symbols: Iterable[str], period: str = "2y") -> Dict[str, pd.DataFrame]:
    from utils.akshare_data import get_history_data

    histories: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        df = _normalize_history(get_history_data(symbol, period))
        if df.empty:
            raise RuntimeError(f"{symbol} 历史行情为空，无法回测")
        histories[symbol] = df
    return histories


def _calendar(histories: Dict[str, pd.DataFrame], start: str, end: str) -> List[pd.Timestamp]:
    start_ts = pd.to_datetime(start)
    end_ts = pd.to_datetime(end)
    dates = sorted(set().union(*(set(df.index) for df in histories.values())))
    return [d for d in dates if start_ts <= d <= end_ts]


def _committee_style_signal(symbol: str, hist: pd.DataFrame) -> Dict[str, Any]:
    from core.decision_optimizer import optimize_committee_decision
    from core.entry_exit_points import compute_entry_exit_points
    from core.regime import format_regime_brief
    from utils.market_metrics import compute_metrics

    metrics = compute_metrics(hist)
    close = _safe_float(metrics.get("current_price"))
    regime_brief = format_regime_brief(metrics, symbol=symbol)
    ret5 = hist["Close"].pct_change(5).iloc[-1] * 100 if len(hist) > 6 else 0.0
    ret20 = hist["Close"].pct_change(20).iloc[-1] * 100 if len(hist) > 21 else 0.0
    rsi = _safe_float(metrics.get("rsi14"), 50.0)
    rvol = _safe_float(metrics.get("rvol"), 1.0)
    ma20 = _safe_float(metrics.get("ma20"))
    ma120 = _safe_float(metrics.get("ma120"))
    quantile = _safe_float(metrics.get("price_quantile_2y"), 0.5)

    score = 50.0
    score += max(-12.0, min(18.0, ret5 * 1.8))
    score += max(-10.0, min(14.0, ret20 * 0.55))
    if ma20 > 0 and ma120 > 0:
        score += max(-14.0, min(14.0, (ma20 / ma120 - 1.0) * 160.0))
    score += max(-10.0, min(10.0, (rvol - 1.0) * 8.0))
    if 45 <= rsi <= 68:
        score += 5.0
    elif rsi > 78:
        score -= 8.0
    elif rsi < 35:
        score -= 4.0
    if quantile >= 0.82:
        score -= 7.0
    elif 0.35 <= quantile <= 0.75:
        score += 4.0

    verdict = "HOLD"
    confidence = 0.50
    alloc = 0
    if score >= 72:
        verdict = "BUY"
        confidence = min(0.92, 0.55 + (score - 70.0) / 60.0)
        alloc = 30_000
    elif score >= 62:
        verdict = "ACCUMULATE"
        confidence = min(0.82, 0.52 + (score - 60.0) / 70.0)
        alloc = 15_000
    elif score <= 38:
        verdict = "TRIM"
        confidence = min(0.80, 0.52 + (40.0 - score) / 80.0)
        alloc = -15_000

    opt = optimize_committee_decision(
        parsed={"verdict": verdict, "confidence": confidence, "alloc_cny": alloc},
        metrics=metrics,
        symbol=symbol,
        regime_brief=regime_brief,
        current_price=close,
        total_assets=100_000.0,
        available_cash=100_000.0,
        position_pct=0.0,
        min_lot_size=_lot_size(symbol),
        target_position_pct=25.0,
        market="a",
        risk_preference="moderate",
    )
    plan = compute_entry_exit_points(
        symbol=symbol,
        current_price=close,
        metrics=metrics,
        regime_brief=regime_brief,
        market="a",
        expected_return_pct=opt.expected_return_pct,
    ).as_dict()
    return {
        "score": round(score, 3),
        "verdict": opt.verdict,
        "confidence": opt.confidence,
        "alloc_cny": opt.alloc_cny,
        "lots": opt.lots,
        "expected_return_pct": opt.expected_return_pct,
        "atr_pct": _safe_float(metrics.get("atr_pct"), _safe_float(plan.get("atr_pct"), 2.0)),
        "entry_exit_points": plan,
        "regime": regime_brief,
    }


def _build_position(
    symbol: str,
    name: str,
    shares: int,
    fill_price: float,
    trade_date: str,
    atr_pct: float,
    params: ExitParams,
) -> Position:
    stop_pct = min(params.max_loss_pct, max(3.0, atr_pct * params.stop_atr_mult))
    hard_stop = fill_price * (1.0 - stop_pct / 100.0)
    risk = max(fill_price - hard_stop, 0.01)
    return Position(
        symbol=symbol,
        name=name,
        shares=shares,
        buy_date=trade_date,
        avg_cost=_round_price(fill_price),
        hard_stop=_round_price(hard_stop),
        effective_stop=_round_price(hard_stop),
        take_profit_1=_round_price(fill_price + risk * params.take_profit_r1),
        take_profit_2=_round_price(fill_price + risk * params.take_profit_r2),
        highest_close=_round_price(fill_price),
        atr_pct=round(atr_pct, 4),
    )


def _update_trailing_stop(pos: Position, close: float, params: ExitParams) -> None:
    pos.highest_close = max(pos.highest_close, _round_price(close))
    trail = pos.highest_close - pos.avg_cost * pos.atr_pct * params.trailing_atr_mult / 100.0
    pos.effective_stop = _round_price(max(pos.effective_stop, pos.hard_stop, trail))


def _sellable(pos: Position, trade_date: str) -> bool:
    return trade_date > pos.buy_date


def _rank_candidates(signals: Dict[str, Dict[str, Any]]) -> List[str]:
    return sorted(
        signals,
        key=lambda s: (
            -_safe_float(signals[s].get("score")),
            -_safe_float(signals[s].get("expected_return_pct")),
            s,
        ),
    )



def _objective_metrics(equity_curve: List[Dict[str, Any]], max_drawdown_pct: float) -> Dict[str, float]:
    """Finite-sample objective: expected return adjusted by drawdown and downside risk."""
    values = [_safe_float(row.get("equity")) for row in equity_curve]
    daily_returns: List[float] = []
    for prev, cur in zip(values, values[1:]):
        if prev > 0:
            daily_returns.append((cur / prev - 1.0) * 100.0)
    if not daily_returns:
        return {
            "expected_daily_return_pct": 0.0,
            "daily_volatility_pct": 0.0,
            "downside_deviation_pct": 0.0,
            "annualized_expected_return_pct": 0.0,
            "objective_score": 0.0,
        }
    mean = sum(daily_returns) / len(daily_returns)
    variance = sum((x - mean) ** 2 for x in daily_returns) / max(len(daily_returns), 1)
    downside = [min(0.0, x) for x in daily_returns]
    downside_variance = sum(x * x for x in downside) / max(len(downside), 1)
    annualized_expected = mean * 252.0
    annualized_downside = math.sqrt(downside_variance) * math.sqrt(252.0)
    risk = max(abs(max_drawdown_pct) + annualized_downside, 0.01)
    return {
        "expected_daily_return_pct": round(mean, 6),
        "daily_volatility_pct": round(math.sqrt(variance), 6),
        "downside_deviation_pct": round(math.sqrt(downside_variance), 6),
        "annualized_expected_return_pct": round(annualized_expected, 4),
        "objective_score": round(annualized_expected / risk, 6),
    }
def run_backtest(
    *,
    histories: Dict[str, pd.DataFrame],
    names: Dict[str, str],
    start: str,
    end: str,
    params: ExitParams,
    signal_cache: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    initial_cash: float = 100_000.0,
    fee_rate: float = 0.0005,
    max_ops_per_symbol_per_day: int = 5,
) -> Dict[str, Any]:
    dates = _calendar(histories, start, end)
    cash = float(initial_cash)
    positions: Dict[str, Position] = {}
    trades: List[Dict[str, Any]] = []
    equity_curve: List[Dict[str, Any]] = []
    symbol_daily_rows: List[Dict[str, Any]] = []

    for d in dates:
        date_str = d.strftime("%Y-%m-%d")
        rows = {s: df.loc[d] for s, df in histories.items() if d in df.index}
        ops_count: Dict[str, int] = {}
        signals: Dict[str, Dict[str, Any]] = {}
        if signal_cache is not None:
            signals = signal_cache.get(date_str, {})
        else:
            for symbol, df in histories.items():
                past = df[df.index < d]
                if len(past) >= 130:
                    signals[symbol] = _committee_style_signal(symbol, past)
        day_start_positions = set(positions)

        # Exits first. A-shares are T+1, so same-day buys cannot be sold.
        for symbol, pos in list(positions.items()):
            row = rows.get(symbol)
            if row is None or ops_count.get(symbol, 0) >= max_ops_per_symbol_per_day:
                continue
            if not _sellable(pos, date_str) or _is_limit_down(symbol, row):
                _update_trailing_stop(pos, _safe_float(row.get("Close")), params)
                continue
            open_p = _safe_float(row.get("Open"))
            low = _safe_float(row.get("Low"))
            high = _safe_float(row.get("High"))
            close = _safe_float(row.get("Close"))
            exit_reason = ""
            fill = 0.0
            shares_to_sell = 0
            if low <= pos.effective_stop:
                exit_reason = "持仓纪律止损"
                fill = min(open_p, pos.effective_stop) if open_p < pos.effective_stop else pos.effective_stop
                shares_to_sell = pos.shares
            elif high >= pos.take_profit_2:
                exit_reason = "第二止盈"
                fill = max(open_p, pos.take_profit_2) if open_p > pos.take_profit_2 else pos.take_profit_2
                shares_to_sell = pos.shares
            elif (not pos.r1_taken) and high >= pos.take_profit_1:
                exit_reason = "第一止盈"
                fill = max(open_p, pos.take_profit_1) if open_p > pos.take_profit_1 else pos.take_profit_1
                shares_to_sell = max(_lot_size(symbol, pos), (pos.shares // 2) // 100 * 100)
                shares_to_sell = min(pos.shares, shares_to_sell)
            elif signals.get(symbol, {}).get("verdict") in {"SELL", "TRIM"} and close < pos.effective_stop * 1.03:
                exit_reason = "委员会风险减仓"
                fill = close
                shares_to_sell = max(_lot_size(symbol, pos), (pos.shares // 2) // 100 * 100)
                shares_to_sell = min(pos.shares, shares_to_sell)

            if shares_to_sell > 0 and fill > 0:
                gross = shares_to_sell * fill
                fee = _commission(gross, fee_rate)
                cash += gross - fee
                pnl = (fill - pos.avg_cost) * shares_to_sell - fee
                trades.append({
                    "date": date_str,
                    "symbol": symbol,
                    "name": pos.name,
                    "side": "SELL",
                    "shares": shares_to_sell,
                    "lots": shares_to_sell // 100,
                    "price": round(fill, 4),
                    "fee": round(fee, 2),
                    "cash_after": round(cash, 2),
                    "pnl": round(pnl, 2),
                    "reason": exit_reason,
                    "stop": pos.effective_stop,
                    "tp1": pos.take_profit_1,
                    "tp2": pos.take_profit_2,
                })
                ops_count[symbol] = ops_count.get(symbol, 0) + 1
                pos.shares -= shares_to_sell
                pos.r1_taken = True
                if pos.shares <= 0:
                    del positions[symbol]
                else:
                    positions[symbol] = pos
                    _update_trailing_stop(pos, close, params)
            else:
                _update_trailing_stop(pos, close, params)

        # Entries after exits: buy strongest candidates, cash constrained.
        for symbol in _rank_candidates(signals):
            if ops_count.get(symbol, 0) >= max_ops_per_symbol_per_day:
                continue
            signal = signals[symbol]
            if symbol in positions:
                continue
            if signal["verdict"] not in {"BUY", "ACCUMULATE"}:
                continue
            if _safe_float(signal.get("score")) < params.min_score_to_buy:
                continue
            row = rows.get(symbol)
            if row is None or _is_limit_up(symbol, row):
                continue
            entry_plan = signal.get("entry_exit_points") or {}
            breakout = _safe_float(entry_plan.get("buy_breakout_price"))
            pullback = _safe_float(entry_plan.get("buy_pullback_price"))
            open_p = _safe_float(row.get("Open"))
            low = _safe_float(row.get("Low"))
            high = _safe_float(row.get("High"))
            close = _safe_float(row.get("Close"))
            fill = 0.0
            reason = ""
            if pullback > 0 and low <= pullback <= high:
                fill = min(max(open_p, pullback), high)
                reason = "回调买入触发"
            elif breakout > 0 and high >= breakout and close >= breakout * 0.995:
                fill = max(open_p, breakout)
                reason = "突破买入触发"
            elif signal["score"] >= params.min_score_to_buy + 8 and close > open_p:
                fill = close
                reason = "委员会强信号收盘买入"
            if fill <= 0:
                continue
            lot = _lot_size(symbol)
            target_cash = min(max(_safe_float(signal.get("alloc_cny")), fill * lot), cash)
            shares = int(target_cash // (fill * lot)) * lot
            if shares < lot:
                continue
            cost = shares * fill
            fee = _commission(cost, fee_rate)
            if cost + fee > cash:
                shares = int(cash // ((fill * lot) * (1 + fee_rate))) * lot
                cost = shares * fill
                fee = _commission(cost, fee_rate)
            if shares < lot or cost + fee > cash:
                continue
            cash -= cost + fee
            pos = _build_position(
                symbol=symbol,
                name=names.get(symbol, symbol),
                shares=shares,
                fill_price=fill,
                trade_date=date_str,
                atr_pct=_safe_float(signal.get("atr_pct"), 2.0),
                params=params,
            )
            positions[symbol] = pos
            ops_count[symbol] = ops_count.get(symbol, 0) + 1
            trades.append({
                "date": date_str,
                "symbol": symbol,
                "name": pos.name,
                "side": "BUY",
                "shares": shares,
                "lots": shares // 100,
                "price": round(fill, 4),
                "fee": round(fee, 2),
                "cash_after": round(cash, 2),
                "reason": reason,
                "score": signal["score"],
                "verdict": signal["verdict"],
                "stop": pos.effective_stop,
                "tp1": pos.take_profit_1,
                "tp2": pos.take_profit_2,
            })

        market_value = 0.0
        symbol_market_values: Dict[str, float] = {}
        for symbol, pos in positions.items():
            row = rows.get(symbol)
            price = _safe_float(row.get("Close")) if row is not None else pos.avg_cost
            value = pos.shares * price
            symbol_market_values[symbol] = value
            market_value += value
        portfolio_equity = cash + market_value
        equity_curve.append({
            "date": date_str,
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "equity": round(portfolio_equity, 2),
            "positions": {s: asdict(p) for s, p in positions.items()},
        })
        for symbol, row in rows.items():
            signal = signals.get(symbol, {})
            pos = positions.get(symbol)
            prev_close = _safe_float(row.get("PrevClose"), 0.0)
            close = _safe_float(row.get("Close"), 0.0)
            symbol_daily_rows.append({
                "date": date_str,
                "symbol": symbol,
                "name": names.get(symbol, symbol),
                "open": round(_safe_float(row.get("Open")), 4),
                "high": round(_safe_float(row.get("High")), 4),
                "low": round(_safe_float(row.get("Low")), 4),
                "close": round(close, 4),
                "change_pct": round((close / prev_close - 1.0) * 100.0, 4) if prev_close > 0 else 0.0,
                "signal_score": signal.get("score"),
                "signal_verdict": signal.get("verdict", ""),
                "signal_alloc_cny": signal.get("alloc_cny", 0),
                "expected_return_pct": signal.get("expected_return_pct"),
                "atr_pct": signal.get("atr_pct"),
                "held_at_start": symbol in day_start_positions,
                "held_at_close": pos is not None,
                "shares": 0 if pos is None else pos.shares,
                "market_value": round(symbol_market_values.get(symbol, 0.0), 2),
                "stop": "" if pos is None else pos.effective_stop,
                "tp1": "" if pos is None else pos.take_profit_1,
                "tp2": "" if pos is None else pos.take_profit_2,
                "cash": round(cash, 2),
                "portfolio_equity": round(portfolio_equity, 2),
            })

    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_cash
    values = [row["equity"] for row in equity_curve]
    peak = values[0] if values else initial_cash
    max_dd = 0.0
    for value in values:
        peak = max(peak, value)
        max_dd = min(max_dd, value / peak - 1.0 if peak else 0.0)
    sell_pnls = [t.get("pnl", 0.0) for t in trades if t["side"] == "SELL"]
    total_return_pct = (final_equity / initial_cash - 1.0) * 100.0
    max_drawdown_pct = max_dd * 100.0
    return_risk_ratio = total_return_pct / max(abs(max_drawdown_pct), 0.01)
    objective = _objective_metrics(equity_curve, max_drawdown_pct)
    return {
        "params": asdict(params),
        "metrics": {
            "initial_cash": round(initial_cash, 2),
            "final_equity": round(final_equity, 2),
            "total_return_pct": round(total_return_pct, 4),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "return_risk_ratio": round(return_risk_ratio, 6),
            "trade_count": len(trades),
            "buy_count": sum(1 for t in trades if t["side"] == "BUY"),
            "sell_count": sum(1 for t in trades if t["side"] == "SELL"),
            "realized_pnl": round(sum(sell_pnls), 2),
            "win_rate": round(sum(1 for x in sell_pnls if x > 0) / len(sell_pnls), 4) if sell_pnls else 0.0,
            **objective,
        },
        "trades": trades,
        "equity_curve": equity_curve,
        "symbol_daily_rows": symbol_daily_rows,
    }


def build_signal_cache(
    *,
    histories: Dict[str, pd.DataFrame],
    start: str,
    end: str,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for d in _calendar(histories, start, end):
        date_str = d.strftime("%Y-%m-%d")
        day: Dict[str, Dict[str, Any]] = {}
        for symbol, df in histories.items():
            past = df[df.index < d]
            if len(past) >= 130:
                day[symbol] = _committee_style_signal(symbol, past)
        cache[date_str] = day
    return cache


def _quantile(values: List[float], q: float, default: float) -> float:
    clean = sorted(v for v in values if math.isfinite(v))
    if not clean:
        return default
    pos = (len(clean) - 1) * min(max(q, 0.0), 1.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return clean[lo]
    return clean[lo] + (clean[hi] - clean[lo]) * (pos - lo)


def _round_candidates(values: Iterable[float], *, lo: float, hi: float, step: float) -> List[float]:
    out = set()
    for value in values:
        rounded = round(round(_safe_float(value) / step) * step, 4)
        out.add(round(min(max(rounded, lo), hi), 4))
    return sorted(out)


def _forward_excursion_samples(
    histories: Dict[str, pd.DataFrame],
    start: str,
    end: str,
    *,
    horizon: int = 5,
) -> Dict[str, List[float]]:
    start_ts = pd.to_datetime(start)
    end_ts = pd.to_datetime(end)
    atr_values: List[float] = []
    mae_values: List[float] = []
    mfe_values: List[float] = []
    score_values: List[float] = []
    for symbol, df in histories.items():
        for idx in range(130, len(df) - 1):
            date = df.index[idx]
            if date < start_ts or date > end_ts:
                continue
            past = df.iloc[:idx]
            signal = _committee_style_signal(symbol, past)
            score_values.append(_safe_float(signal.get("score"), 60.0))
            atr = max(_safe_float(signal.get("atr_pct"), 2.0), 0.5)
            atr_values.append(atr)
            entry = _safe_float(df["Close"].iloc[idx - 1])
            if entry <= 0:
                continue
            future = df.iloc[idx:min(len(df), idx + horizon)]
            if future.empty:
                continue
            mae = max(0.0, (entry - _safe_float(future["Low"].min())) / entry * 100.0)
            mfe = max(0.0, (_safe_float(future["High"].max()) - entry) / entry * 100.0)
            mae_values.append(mae)
            mfe_values.append(mfe)
    return {
        "atr_pct": atr_values,
        "mae_pct": mae_values,
        "mfe_pct": mfe_values,
        "signal_score": score_values,
    }


def build_discrete_param_grid(
    histories: Dict[str, pd.DataFrame],
    *,
    start: str,
    end: str,
) -> Tuple[List[ExitParams], Dict[str, Any]]:
    """Build finite parameter candidates from recent market samples.

    Discrete math formulation:
      Theta = S_loss x S_stop x S_r1 x S_r2 x S_trail x S_score
      theta* = argmax_{theta in Theta} E[R(theta)] / max(|MDD(theta)|, eps)

    Candidate sets come from empirical ATR, MAE and MFE quantiles observed in
    the calibration window, so the bounds are data-generated rather than fixed
    hand-picked constants.
    """
    samples = _forward_excursion_samples(histories, start, end, horizon=5)
    atr_q = [_quantile(samples["atr_pct"], q, 3.0) for q in (0.25, 0.50, 0.75)]
    mae_q = [_quantile(samples["mae_pct"], q, 4.0) for q in (0.50, 0.70, 0.85)]
    mfe_q = [_quantile(samples["mfe_pct"], q, 8.0) for q in (0.55, 0.70, 0.85)]
    score_q = [_quantile(samples["signal_score"], q, 64.0) for q in (0.55, 0.65, 0.75)]

    max_loss_set = _round_candidates(
        [max(3.0, x) for x in mae_q] + [min(10.0, max(3.0, x * 1.15)) for x in mae_q],
        lo=3.0,
        hi=10.0,
        step=0.5,
    )
    stop_atr_set = _round_candidates(
        [loss / max(atr, 0.5) for loss in max_loss_set for atr in atr_q],
        lo=0.8,
        hi=3.2,
        step=0.2,
    )
    r1_set = _round_candidates(
        [mfe / max(loss, 0.5) * 0.45 for mfe in mfe_q for loss in max_loss_set],
        lo=0.8,
        hi=2.5,
        step=0.25,
    )
    r2_set = _round_candidates(
        [mfe / max(loss, 0.5) * 0.95 for mfe in mfe_q for loss in max_loss_set],
        lo=1.5,
        hi=5.0,
        step=0.25,
    )
    trail_atr_set = _round_candidates(
        [mae / max(atr, 0.5) for mae in mae_q for atr in atr_q] + [2.0, 2.5, 3.0],
        lo=1.0,
        hi=4.0,
        step=0.25,
    )
    score_set = _round_candidates(score_q + [60.0, 64.0, 68.0], lo=55.0, hi=80.0, step=1.0)

    params: List[ExitParams] = []
    seen = set()
    for max_loss in max_loss_set:
        for stop_atr in stop_atr_set:
            for r1 in r1_set:
                for r2 in r2_set:
                    if r2 <= r1:
                        continue
                    for trail in trail_atr_set:
                        for score in score_set:
                            item = ExitParams(max_loss, stop_atr, r1, r2, trail, score)
                            key = tuple(asdict(item).values())
                            if key not in seen:
                                seen.add(key)
                                params.append(item)
    diagnostics = {
        "formulation": "argmax_theta E[return(theta)] / max(abs(max_drawdown(theta)), epsilon)",
        "sample_count": {k: len(v) for k, v in samples.items()},
        "quantiles": {
            "atr_pct": [round(x, 4) for x in atr_q],
            "mae_pct": [round(x, 4) for x in mae_q],
            "mfe_pct": [round(x, 4) for x in mfe_q],
            "signal_score": [round(x, 4) for x in score_q],
        },
        "candidate_set_sizes": {
            "max_loss_pct": len(max_loss_set),
            "stop_atr_mult": len(stop_atr_set),
            "take_profit_r1": len(r1_set),
            "take_profit_r2": len(r2_set),
            "trailing_atr_mult": len(trail_atr_set),
            "min_score_to_buy": len(score_set),
            "theta_count": len(params),
        },
        "candidate_sets": {
            "max_loss_pct": max_loss_set,
            "stop_atr_mult": stop_atr_set,
            "take_profit_r1": r1_set,
            "take_profit_r2": r2_set,
            "trailing_atr_mult": trail_atr_set,
            "min_score_to_buy": score_set,
        },
    }
    return params, diagnostics


def optimize_exit_params(
    *,
    histories: Dict[str, pd.DataFrame],
    names: Dict[str, str],
    start: str,
    end: str,
    signal_cache: Dict[str, Dict[str, Dict[str, Any]]],
    initial_cash: float,
    fee_rate: float,
    max_ops_per_symbol_per_day: int,
    param_grid: List[ExitParams],
) -> Dict[str, Any]:
    best: Optional[Dict[str, Any]] = None
    all_results: List[Dict[str, Any]] = []
    for params in param_grid:
        result = run_backtest(
            histories=histories,
            names=names,
            start=start,
            end=end,
            params=params,
            signal_cache=signal_cache,
            initial_cash=initial_cash,
            fee_rate=fee_rate,
            max_ops_per_symbol_per_day=max_ops_per_symbol_per_day,
        )
        score = result["metrics"].get("objective_score", result["metrics"]["return_risk_ratio"])
        compact = {
            "score": round(score, 6),
            "params": result["params"],
            "metrics": result["metrics"],
        }
        all_results.append(compact)
        if best is None or score > best["score"]:
            best = {"score": score, "result": result}
    if best is None:
        raise ValueError("empty parameter grid")
    all_results.sort(key=lambda row: row["score"], reverse=True)
    return {
        "best": best["result"],
        "top_results": all_results[:10],
        "trial_count": len(all_results),
    }



def update_env_exit_params(params: Dict[str, Any], env_path: Path) -> Dict[str, Any]:
    """Update only the exit-model keys in .env, preserving all other secrets."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = existing.splitlines()
    rendered = {
        key: str(params[param_name])
        for param_name, key in ENV_PARAM_KEYS.items()
        if param_name in params
    }
    seen = set()
    updated_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            updated_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in rendered:
            updated_lines.append(f"{key}={rendered[key]}")
            seen.add(key)
        else:
            updated_lines.append(line)

    missing = [key for key in rendered if key not in seen]
    if missing:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        updated_lines.append("# A股持仓止盈止损模型参数（由离散回测每周自动更新）")
        for key in missing:
            updated_lines.append(f"{key}={rendered[key]}")

    env_path.write_text("\n".join(updated_lines).rstrip() + "\n", encoding="utf-8")
    return {
        "updated": True,
        "env_path": str(env_path),
        "keys": sorted(rendered),
    }


def update_env_sector_exit_policies(policies: Dict[str, Dict[str, Any]], env_path: Path) -> Dict[str, Any]:
    """Update sector-specific exit policies in .env as one JSON value."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    rendered = json.dumps(policies, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    lines = existing.splitlines()
    updated_lines: List[str] = []
    seen = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key == SECTOR_POLICY_ENV_KEY:
                updated_lines.append(f"{SECTOR_POLICY_ENV_KEY}={rendered}")
                seen = True
                continue
        updated_lines.append(line)
    if not seen:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        updated_lines.append("# A股板块级持仓止盈止损模型参数（由离散回测每周自动更新）")
        updated_lines.append(f"{SECTOR_POLICY_ENV_KEY}={rendered}")
    env_path.write_text("\n".join(updated_lines).rstrip() + "\n", encoding="utf-8")
    return {
        "updated": True,
        "env_path": str(env_path),
        "key": SECTOR_POLICY_ENV_KEY,
        "sector_count": len(policies),
    }
def _default_dates(days: int) -> Tuple[str, str]:
    end = datetime.now().date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    df = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="", help="逗号分隔A股代码；留空则不运行回测")
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--fee-rate", type=float, default=0.0005, help="万五=0.0005")
    parser.add_argument("--max-ops", type=int, default=5)
    parser.add_argument("--out", default=str(ROOT / "reports" / "ashare_committee_exit_backtest.json"))
    parser.add_argument("--trades-csv", default=str(ROOT / "reports" / "ashare_committee_exit_trades.csv"))
    parser.add_argument("--daily-csv", default=str(ROOT / "reports" / "ashare_committee_exit_daily_samples.csv"))
    parser.add_argument("--update-env", action="store_true", help="把最优止盈止损参数写回本地 .env")
    parser.add_argument("--env-path", default=str(ROOT / ".env"))
    args = parser.parse_args()

    start, end = (args.start, args.end) if args.start and args.end else _default_dates(args.days)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("请通过 --symbols 或 INVEST_EXIT_PARAM_OPT_SYMBOLS 指定A股样本")
    names = {s: DEFAULT_SYMBOLS.get(s, s) for s in symbols}
    histories = _fetch_histories(symbols, period="2y")
    signal_cache = build_signal_cache(histories=histories, start=start, end=end)
    param_grid, grid_diagnostics = build_discrete_param_grid(histories, start=start, end=end)
    result = optimize_exit_params(
        histories=histories,
        names=names,
        start=start,
        end=end,
        signal_cache=signal_cache,
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        max_ops_per_symbol_per_day=args.max_ops,
        param_grid=param_grid,
    )
    result["discrete_optimization"] = grid_diagnostics
    result["config"] = {
        "symbols": names,
        "start": start,
        "end": end,
        "initial_cash": args.initial_cash,
        "fee_rate": args.fee_rate,
        "max_ops_per_symbol_per_day": args.max_ops,
        "a_share_rules": {
            "lot_size": 100,
            "star_market_first_buy_lot": 200,
            "t_plus_1": True,
            "limit_up_down_blocked": True,
        },
    }
    if args.update_env:
        result["env_update"] = update_env_exit_params(result["best"]["params"], Path(args.env_path))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(result["best"]["trades"], Path(args.trades_csv))
    _write_csv(result["best"]["symbol_daily_rows"], Path(args.daily_csv))

    best = result["best"]
    print(json.dumps({
        "output": str(out),
        "trades_csv": args.trades_csv,
        "daily_csv": args.daily_csv,
        "config": result["config"],
        "best_params": best["params"],
        "metrics": best["metrics"],
        "trades": best["trades"],
        "top_results": result["top_results"][:5],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
