"""Compare paper-inspired OHLCV factors with the current A-share baseline.

The factor specifications are fixed before the two-month evaluation window.
They are deliberately simple, auditable proxies for papers whose full models
need a much larger universe or unavailable fundamentals.  Signals only use
bars strictly before the execution date; orders execute at the next available
open under the same cash, lot, T+1, limit, and fee constraints as the current
committee backtest.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_ashare_committee_exit import (  # noqa: E402
    _calendar,
    _is_limit_down,
    _is_limit_up,
    build_signal_cache,
    run_backtest,
)
from scripts.compare_upgrade_profitability import (  # noqa: E402
    DEFAULT_SNAPSHOT_PATH,
    _configured_a_share_symbols,
    paired_block_bootstrap_return_difference,
    prepare_history_snapshot,
    production_exit_params,
)
from utils.safe_persistence import atomic_write_json  # noqa: E402


PAPER_REFERENCES: Dict[str, Dict[str, str]] = {
    "a_share_behavioral": {
        "title": "Interpretable Factor Decomposition for Decision Intelligence in Large-Scale Financial Markets",
        "url": "https://arxiv.org/abs/2606.12843",
        "use": "A-share monthly momentum, turnover, and volatility hierarchy; OHLCV-only proxy, not XGBoost replication.",
    },
    "ema_trend": {
        "title": "Breaking the Trend: How to Avoid Cherry-Picked Signals",
        "url": "https://arxiv.org/abs/2504.10914",
        "use": "One fixed 100-day EMA return signal rather than a parameter basket.",
    },
    "drift_reversal": {
        "title": "Discovery of a 13-Sharpe OOS Factor: Drift Regimes Unlock Hidden Cross-Sectional Predictability",
        "url": "https://arxiv.org/abs/2511.12490",
        "use": "63-day positive-day drift gate plus five-day reversal; value leg omitted because point-in-time value data are unavailable.",
    },
    "spatio_temporal": {
        "title": "Spatio-Temporal Momentum: Jointly Learning Time-Series and Cross-Sectional Strategies",
        "url": "https://arxiv.org/abs/2302.10175",
        "use": "Transparent linear time-series/cross-sectional momentum proxy with low turnover, not the paper's neural network.",
    },
    "low_volatility": {
        "title": "The Low-volatility Anomaly and the Adaptive Multi-Factor Model",
        "url": "https://arxiv.org/abs/2003.08302",
        "use": "Long-only low-volatility ranking gated by a positive 120-day trend.",
    },
    "industry_trend": {
        "title": "Refining and Robust Backtesting of A Century of Profitable Industry Trends",
        "url": "https://arxiv.org/abs/2412.14361",
        "use": "Long-only industry trend proxy using locally cached Eastmoney sectors.",
    },
}


@dataclass(frozen=True)
class FactorSpec:
    key: str
    label: str
    rebalance_days: int
    top_k: int
    gross_exposure: float = 0.95
    max_weight: float = 0.35
    spatio_weight: float = 0.60
    target_blend: float = 1.0
    min_trade_weight: float = 0.0


FACTOR_SPECS: Tuple[FactorSpec, ...] = (
    FactorSpec("a_share_behavioral", "A股行为因子（月频动量+换手代理）", 20, 4),
    FactorSpec("ema_trend", "单EMA趋势+波动率缩放", 5, 4),
    FactorSpec("drift_reversal", "漂移状态短期反转", 5, 4),
    FactorSpec("spatio_temporal", "时序+横截面动量", 10, 4),
    FactorSpec("low_volatility", "低波动趋势", 10, 4),
    FactorSpec("industry_trend", "行业趋势轮动", 20, 4),
)
PASSIVE_SPEC = FactorSpec("equal_weight_buy_hold", "同池等权买入持有", 10_000, 100)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _return(close: pd.Series, periods: int) -> float:
    if len(close) <= periods:
        return float("nan")
    base = _safe_float(close.iloc[-periods - 1])
    latest = _safe_float(close.iloc[-1])
    return latest / base - 1.0 if base > 0 and latest > 0 else float("nan")


def _annualized_vol(returns: pd.Series, periods: int) -> float:
    values = returns.tail(periods).dropna()
    if len(values) < max(10, periods // 2):
        return float("nan")
    return float(values.std(ddof=1) * math.sqrt(252.0))


def build_feature_table(
    histories: Mapping[str, pd.DataFrame],
    decision_date: pd.Timestamp,
) -> pd.DataFrame:
    """Build point-in-time features using bars strictly before decision_date."""
    records: List[Dict[str, Any]] = []
    for symbol, frame in histories.items():
        past = frame[frame.index < decision_date]
        if len(past) < 130:
            continue
        close = pd.to_numeric(past["Close"], errors="coerce").dropna()
        if len(close) < 130:
            continue
        returns = close.pct_change()
        volume = pd.to_numeric(past.get("Volume", pd.Series(index=past.index, dtype=float)), errors="coerce")
        volume20 = _safe_float(volume.tail(20).mean(), float("nan"))
        volume120 = _safe_float(volume.tail(120).mean(), float("nan"))
        volume_ratio = volume20 / volume120 if volume20 > 0 and volume120 > 0 else 1.0
        ema100_price = _safe_float(close.ewm(span=100, adjust=False).mean().iloc[-1])
        ema120_price = _safe_float(close.ewm(span=120, adjust=False).mean().iloc[-1])
        ema_return100 = _safe_float(returns.ewm(span=100, adjust=False).mean().iloc[-1])
        positive63 = _safe_float((returns.tail(63) > 0).mean(), float("nan"))
        records.append({
            "symbol": symbol,
            "close": _safe_float(close.iloc[-1]),
            "ret5": _return(close, 5),
            "ret20": _return(close, 20),
            "ret60": _return(close, 60),
            "ret120": _return(close, 120),
            "ret252": _return(close, 252),
            "vol20": _annualized_vol(returns, 20),
            "vol60": _annualized_vol(returns, 60),
            "ema_return100": ema_return100,
            "above_ema100": close.iloc[-1] / ema100_price - 1.0 if ema100_price > 0 else float("nan"),
            "above_ema120": close.iloc[-1] / ema120_price - 1.0 if ema120_price > 0 else float("nan"),
            "positive_day_ratio63": positive63,
            "volume_ratio20_120": volume_ratio,
        })
    if not records:
        return pd.DataFrame()
    return pd.DataFrame.from_records(records).set_index("symbol").replace([np.inf, -np.inf], np.nan)


def _rank_high(series: pd.Series) -> pd.Series:
    return series.rank(method="average", pct=True, ascending=True)


def _select_top(scores: pd.Series, top_k: int) -> List[str]:
    clean = scores.replace([np.inf, -np.inf], np.nan).dropna()
    return list(clean.sort_values(ascending=False).head(max(1, int(top_k))).index)


def _behavioral_score(features: pd.DataFrame) -> pd.Series:
    momentum = (
        0.35 * _rank_high(features["ret60"])
        + 0.30 * _rank_high(features["ret120"])
        + 0.20 * _rank_high(features["ret252"])
        + 0.15 * _rank_high(features["ret20"])
    )
    low_turnover_proxy = _rank_high(-features["volume_ratio20_120"])
    low_volatility = _rank_high(-features["vol20"])
    score = 0.65 * momentum + 0.25 * low_turnover_proxy + 0.10 * low_volatility
    return score.where((features["ret60"] > 0) & (features["above_ema100"] > 0))


def _spatio_temporal_score(features: pd.DataFrame) -> pd.Series:
    temporal = (
        features["ret20"] / features["vol20"].clip(lower=0.08)
        + features["ret60"] / features["vol60"].clip(lower=0.08)
        + features["ret120"] / features["vol60"].clip(lower=0.08)
    ) / 3.0
    score = 0.55 * _rank_high(temporal) + 0.45 * _rank_high(features["ret60"])
    return score.where((features["ret20"] > 0) & (features["ret60"] > 0))


def _capped_inverse_vol_weights(
    selected: Sequence[str],
    features: pd.DataFrame,
    *,
    gross_exposure: float,
    max_weight: float,
) -> Dict[str, float]:
    if not selected:
        return {}
    risk = {
        symbol: max(_safe_float(features.at[symbol, "vol20"], 0.30), 0.08)
        for symbol in selected
    }
    raw = {symbol: 1.0 / value for symbol, value in risk.items()}
    weights: Dict[str, float] = {}
    free = set(raw)
    remaining = gross_exposure
    while free and remaining > 0:
        free_raw = sum(raw[symbol] for symbol in free)
        proposed = {
            symbol: remaining * raw[symbol] / free_raw
            for symbol in free
        }
        over_cap = {symbol for symbol, value in proposed.items() if value > max_weight + 1e-12}
        if not over_cap:
            weights.update(proposed)
            break
        for symbol in over_cap:
            weights[symbol] = max_weight
            free.remove(symbol)
            remaining -= max_weight
    for symbol in free:
        weights.setdefault(symbol, 0.0)
    total = sum(weights.values())
    if total > gross_exposure and total > 0:
        weights = {symbol: value * gross_exposure / total for symbol, value in weights.items()}
    return {symbol: round(value, 10) for symbol, value in weights.items() if value > 0}


def _load_sector_mapping(symbols: Sequence[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    cache_path = ROOT / "data" / "sector_cache.json"
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            mapping.update({str(k): str(v) for k, v in dict(payload.get("mapping") or {}).items() if v})
        except Exception:
            pass
    config_path = ROOT / "jobs" / "market_monitor_config.json"
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            for row in list(payload.get("holdings") or []) + list(payload.get("watchlist") or []):
                symbol = str(row.get("symbol") or "")
                sector = str(row.get("sector") or row.get("industry") or "").strip()
                if symbol and sector:
                    mapping.setdefault(symbol, sector)
        except Exception:
            pass
    return {symbol: mapping[symbol] for symbol in symbols if symbol in mapping}


def factor_target_weights(
    spec: FactorSpec,
    features: pd.DataFrame,
    *,
    sectors: Mapping[str, str] | None = None,
) -> Dict[str, float]:
    if features.empty:
        return {}
    f = features.copy()
    score = pd.Series(index=f.index, dtype=float)

    if spec.key == "equal_weight_buy_hold":
        weight = spec.gross_exposure / len(f)
        return {symbol: round(weight, 10) for symbol in f.index}
    if spec.key == "a_share_behavioral":
        score = _behavioral_score(f)
    elif spec.key == "ema_trend":
        score = _rank_high(f["ema_return100"] / f["vol20"].clip(lower=0.08))
        score = score.where((f["ema_return100"] > 0) & (f["above_ema100"] > 0))
    elif spec.key == "drift_reversal":
        reversal = _rank_high(-f["ret5"])
        medium_trend = _rank_high(f["ret60"] / f["vol60"].clip(lower=0.08))
        score = 0.65 * reversal + 0.35 * medium_trend
        score = score.where((f["positive_day_ratio63"] >= 0.60) & (f["ret60"] > 0))
    elif spec.key == "spatio_temporal":
        score = _spatio_temporal_score(f)
    elif spec.key.startswith("hybrid_"):
        spatio = _spatio_temporal_score(f)
        behavioral = _behavioral_score(f)
        both = pd.concat([spatio.rename("spatio"), behavioral.rename("behavioral")], axis=1)
        score = (
            spec.spatio_weight * both["spatio"]
            + (1.0 - spec.spatio_weight) * both["behavioral"]
        ).where(both.notna().all(axis=1))
    elif spec.key == "low_volatility":
        score = 0.70 * _rank_high(-f["vol60"]) + 0.30 * _rank_high(f["ret120"])
        score = score.where((f["ret120"] > 0) & (f["above_ema120"] > 0))
    elif spec.key == "industry_trend":
        sectors = sectors or {}
        available = [symbol for symbol in f.index if sectors.get(symbol)]
        if not available:
            return {}
        sector_rows = f.loc[available].assign(sector=[sectors[symbol] for symbol in available])
        sector_score = sector_rows.groupby("sector")[["ret20", "ret60", "ret120"]].median().mean(axis=1)
        top_sectors = list(sector_score[sector_score > 0].sort_values(ascending=False).head(2).index)
        selected: List[str] = []
        for sector in top_sectors:
            members = sector_rows[sector_rows["sector"] == sector]
            member_score = members["ret60"] / members["vol60"].clip(lower=0.08)
            selected.extend(_select_top(member_score, 2))
        selected = selected[:spec.top_k]
        return _capped_inverse_vol_weights(
            selected,
            f,
            gross_exposure=spec.gross_exposure,
            max_weight=spec.max_weight,
        )
    else:
        raise ValueError(f"unknown factor specification: {spec.key}")

    selected = _select_top(score, spec.top_k)
    return _capped_inverse_vol_weights(
        selected,
        f,
        gross_exposure=spec.gross_exposure,
        max_weight=spec.max_weight,
    )


def _lot_size(symbol: str, held: bool) -> int:
    return 100 if held or not symbol.startswith("688") else 200


def _metrics(
    equity_curve: Sequence[Dict[str, Any]],
    trades: Sequence[Dict[str, Any]],
    *,
    initial_cash: float,
) -> Dict[str, Any]:
    values = [_safe_float(row.get("equity"), initial_cash) for row in equity_curve]
    final_equity = values[-1] if values else initial_cash
    peak = initial_cash
    max_drawdown = 0.0
    returns: List[float] = []
    previous = initial_cash
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1.0 if peak > 0 else 0.0)
        if previous > 0:
            returns.append(value / previous - 1.0)
        previous = value
    mean = float(np.mean(returns)) if returns else 0.0
    downside = [min(0.0, value) for value in returns]
    downside_dev = math.sqrt(sum(value * value for value in downside) / len(downside)) if downside else 0.0
    sortino = mean * 252.0 / (downside_dev * math.sqrt(252.0)) if downside_dev > 1e-12 else 0.0
    traded_notional = sum(_safe_float(row.get("notional")) for row in trades)
    fees = sum(_safe_float(row.get("fee")) for row in trades)
    average_equity = float(np.mean(values)) if values else initial_cash
    exposure = [
        _safe_float(row.get("market_value")) / _safe_float(row.get("equity"), 1.0)
        for row in equity_curve
        if _safe_float(row.get("equity")) > 0
    ]
    midpoint = max(0, len(values) // 2 - 1)
    first_half_end = values[midpoint] if values else initial_cash
    return {
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity / initial_cash - 1.0) * 100.0, 4),
        "first_half_return_pct": round((first_half_end / initial_cash - 1.0) * 100.0, 4),
        "second_half_return_pct": round((final_equity / first_half_end - 1.0) * 100.0, 4) if first_half_end > 0 else 0.0,
        "max_drawdown_pct": round(max_drawdown * 100.0, 4),
        "sortino_ratio": round(sortino, 6),
        "turnover_pct": round(traded_notional / average_equity * 100.0, 4) if average_equity > 0 else 0.0,
        "total_fees_cny": round(fees, 2),
        "trade_count": len(trades),
        "buy_count": sum(1 for row in trades if row.get("side") == "BUY"),
        "sell_count": sum(1 for row in trades if row.get("side") == "SELL"),
        "average_exposure_pct": round(float(np.mean(exposure)) * 100.0, 4) if exposure else 0.0,
    }


def run_factor_backtest(
    *,
    spec: FactorSpec,
    histories: Dict[str, pd.DataFrame],
    start: str,
    end: str,
    sectors: Mapping[str, str] | None = None,
    initial_cash: float = 100_000.0,
    fee_rate: float = 0.0005,
) -> Dict[str, Any]:
    dates = _calendar(histories, start, end)
    cash = float(initial_cash)
    positions: Dict[str, Dict[str, Any]] = {}
    last_close: Dict[str, float] = {}
    previous_target_weights: Dict[str, float] = {}
    trades: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []

    for symbol, history in histories.items():
        prior = history[history.index < pd.to_datetime(start)]
        if not prior.empty:
            last_close[symbol] = _safe_float(prior["Close"].iloc[-1])

    for day_index, day in enumerate(dates):
        date_str = day.strftime("%Y-%m-%d")
        rows = {symbol: frame.loc[day] for symbol, frame in histories.items() if day in frame.index}
        if day_index % max(1, spec.rebalance_days) == 0:
            features = build_feature_table(histories, day)
            raw_weights = factor_target_weights(spec, features, sectors=sectors)
            blend = min(max(spec.target_blend, 0.0), 1.0)
            if previous_target_weights and blend < 1.0:
                all_symbols = set(previous_target_weights) | set(raw_weights)
                weights = {
                    symbol: (1.0 - blend) * previous_target_weights.get(symbol, 0.0)
                    + blend * raw_weights.get(symbol, 0.0)
                    for symbol in all_symbols
                }
                weights = {symbol: value for symbol, value in weights.items() if value >= 0.005}
            else:
                weights = raw_weights
            previous_target_weights = dict(weights)
            opening_equity = cash + sum(
                position["shares"] * _safe_float(
                    rows.get(symbol, {}).get("Open") if symbol in rows else None,
                    last_close.get(symbol, position["avg_cost"]),
                )
                for symbol, position in positions.items()
            )
            desired_shares: Dict[str, int] = {}
            for symbol, weight in weights.items():
                row = rows.get(symbol)
                open_price = _safe_float(row.get("Open")) if row is not None else 0.0
                if open_price <= 0:
                    continue
                lot = _lot_size(symbol, symbol in positions)
                desired_shares[symbol] = int((opening_equity * weight) // (open_price * lot)) * lot

            for symbol, position in list(positions.items()):
                current = int(position["shares"])
                desired = max(0, desired_shares.get(symbol, 0))
                shares = current - desired
                row = rows.get(symbol)
                if shares < 100 or row is None or position["buy_date"] >= date_str or _is_limit_down(symbol, row):
                    continue
                shares = (shares // 100) * 100
                price = _safe_float(row.get("Open"))
                if (
                    desired > 0
                    and opening_equity > 0
                    and shares * price / opening_equity < spec.min_trade_weight
                ):
                    continue
                gross = shares * price
                fee = gross * fee_rate
                cash += gross - fee
                position["shares"] -= shares
                trades.append({
                    "date": date_str,
                    "symbol": symbol,
                    "side": "SELL",
                    "shares": shares,
                    "price": round(price, 4),
                    "notional": round(gross, 2),
                    "fee": round(fee, 2),
                    "reason": f"{spec.key}:rebalance",
                })
                if position["shares"] <= 0:
                    del positions[symbol]

            for symbol in sorted(weights, key=weights.get, reverse=True):
                row = rows.get(symbol)
                if row is None or _is_limit_up(symbol, row):
                    continue
                current = int(positions.get(symbol, {}).get("shares", 0))
                desired = desired_shares.get(symbol, 0)
                additional = desired - current
                lot = _lot_size(symbol, symbol in positions)
                additional = (additional // lot) * lot
                price = _safe_float(row.get("Open"))
                if additional < lot or price <= 0:
                    continue
                if (
                    current > 0
                    and opening_equity > 0
                    and additional * price / opening_equity < spec.min_trade_weight
                ):
                    continue
                affordable = int(cash // (price * lot * (1.0 + fee_rate))) * lot
                shares = min(additional, affordable)
                if shares < lot:
                    continue
                gross = shares * price
                fee = gross * fee_rate
                cash -= gross + fee
                if symbol in positions:
                    old_shares = positions[symbol]["shares"]
                    old_cost = positions[symbol]["avg_cost"]
                    positions[symbol]["shares"] += shares
                    positions[symbol]["avg_cost"] = (old_shares * old_cost + gross) / (old_shares + shares)
                    positions[symbol]["buy_date"] = date_str
                else:
                    positions[symbol] = {"shares": shares, "avg_cost": price, "buy_date": date_str}
                trades.append({
                    "date": date_str,
                    "symbol": symbol,
                    "side": "BUY",
                    "shares": shares,
                    "price": round(price, 4),
                    "notional": round(gross, 2),
                    "fee": round(fee, 2),
                    "reason": f"{spec.key}:rebalance",
                })

        market_value = 0.0
        for symbol, position in positions.items():
            row = rows.get(symbol)
            close = _safe_float(row.get("Close")) if row is not None else last_close.get(symbol, position["avg_cost"])
            if close > 0:
                last_close[symbol] = close
            market_value += position["shares"] * close
        for symbol, row in rows.items():
            close = _safe_float(row.get("Close"))
            if close > 0:
                last_close[symbol] = close
        curve.append({
            "date": date_str,
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "equity": round(cash + market_value, 2),
        })

    return {
        "factor": {
            "key": spec.key,
            "label": spec.label,
            "paper": PAPER_REFERENCES.get(spec.key),
            "rebalance_days": spec.rebalance_days,
            "top_k": spec.top_k,
            "gross_exposure": spec.gross_exposure,
            "max_weight": spec.max_weight,
            "spatio_weight": spec.spatio_weight,
            "target_blend": spec.target_blend,
            "min_trade_weight": spec.min_trade_weight,
        },
        "metrics": _metrics(curve, trades, initial_cash=initial_cash),
        "trades": trades,
        "equity_curve": curve,
    }


def compare_factors(
    *,
    histories: Dict[str, pd.DataFrame],
    symbols: List[str],
    start: str,
    end: str,
    snapshot_info: Dict[str, Any],
    initial_cash: float = 100_000.0,
    fee_rate: float = 0.0005,
    max_ops: int = 5,
) -> Dict[str, Any]:
    signal_cache = build_signal_cache(histories=histories, start=start, end=end)
    baseline_result = run_backtest(
        histories={symbol: frame.copy(deep=True) for symbol, frame in histories.items()},
        names={symbol: symbol for symbol in symbols},
        start=start,
        end=end,
        params=production_exit_params(),
        signal_cache=signal_cache,
        initial_cash=initial_cash,
        fee_rate=fee_rate,
        max_ops_per_symbol_per_day=max_ops,
        strategy_mode="legacy",
        missing_price_policy="carry_forward",
    )
    baseline_metrics = baseline_result["metrics"]
    sectors = _load_sector_mapping(symbols)
    passive_result = run_factor_backtest(
        spec=PASSIVE_SPEC,
        histories=histories,
        start=start,
        end=end,
        sectors=sectors,
        initial_cash=initial_cash,
        fee_rate=fee_rate,
    )
    passive_metrics = passive_result["metrics"]
    candidates: List[Dict[str, Any]] = []
    for spec in FACTOR_SPECS:
        result = run_factor_backtest(
            spec=spec,
            histories=histories,
            start=start,
            end=end,
            sectors=sectors,
            initial_cash=initial_cash,
            fee_rate=fee_rate,
        )
        metrics = result["metrics"]
        difference = _safe_float(metrics.get("total_return_pct")) - _safe_float(baseline_metrics.get("total_return_pct"))
        bootstrap = paired_block_bootstrap_return_difference(
            baseline_result["equity_curve"],
            result["equity_curve"],
        )
        candidates.append({
            "factor": result["factor"],
            "metrics": metrics,
            "difference_vs_current": {
                "return_percentage_points": round(difference, 6),
                "turnover_percentage_points": round(
                    _safe_float(metrics.get("turnover_pct")) - _safe_float(baseline_metrics.get("turnover_pct")),
                    6,
                ),
                "max_drawdown_percentage_points": round(
                    _safe_float(metrics.get("max_drawdown_pct")) - _safe_float(baseline_metrics.get("max_drawdown_pct")),
                    6,
                ),
            },
            "difference_vs_equal_weight": {
                "return_percentage_points": round(
                    _safe_float(metrics.get("total_return_pct"))
                    - _safe_float(passive_metrics.get("total_return_pct")),
                    6,
                ),
                "turnover_percentage_points": round(
                    _safe_float(metrics.get("turnover_pct"))
                    - _safe_float(passive_metrics.get("turnover_pct")),
                    6,
                ),
            },
            "paired_block_bootstrap": bootstrap,
            "eligible_for_ranking": (
                difference > 0
                and _safe_float(metrics.get("average_exposure_pct")) >= 20.0
                and int(metrics.get("trade_count") or 0) > 0
            ),
            "trades": result["trades"],
            "equity_curve": result["equity_curve"],
        })
    ranked = sorted(
        (row for row in candidates if row["eligible_for_ranking"]),
        key=lambda row: (
            -_safe_float(row["metrics"].get("total_return_pct")),
            -_safe_float(row["metrics"].get("sortino_ratio")),
            _safe_float(row["metrics"].get("turnover_pct")),
        ),
    )
    top_three = []
    for rank, row in enumerate(ranked[:3], start=1):
        top_three.append({
            "rank": rank,
            "factor": row["factor"],
            "metrics": row["metrics"],
            "difference_vs_current": row["difference_vs_current"],
            "difference_vs_equal_weight": row["difference_vs_equal_weight"],
            "paired_block_bootstrap": row["paired_block_bootstrap"],
        })
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "period": {"start": start, "end": end},
        "universe": {
            "market": "A-share",
            "symbol_count": len(symbols),
            "sector_mapped_count": len(sectors),
        },
        "history_snapshot": snapshot_info,
        "constraints": {
            "initial_cash_cny": initial_cash,
            "fee_rate": fee_rate,
            "a_share_t_plus_1": True,
            "limit_up_down": True,
            "integer_lots": True,
            "star_market_first_buy_shares": 200,
            "max_operations_per_symbol_per_day": 1,
            "signal_uses_only_prior_bars": True,
            "parameters_tuned_on_evaluation_period": False,
        },
        "current_baseline": {
            key: baseline_metrics.get(key)
            for key in (
                "final_equity",
                "total_return_pct",
                "max_drawdown_pct",
                "sortino_ratio",
                "turnover_pct",
                "total_fees_cny",
                "trade_count",
            )
        },
        "equal_weight_buy_hold": passive_metrics,
        "top_three": top_three,
        "candidate_summary": [
            {
                "factor": row["factor"],
                "metrics": row["metrics"],
                "difference_vs_current": row["difference_vs_current"],
                "difference_vs_equal_weight": row["difference_vs_equal_weight"],
                "paired_block_bootstrap": row["paired_block_bootstrap"],
                "eligible_for_ranking": row["eligible_for_ranking"],
            }
            for row in sorted(candidates, key=lambda item: -_safe_float(item["metrics"].get("total_return_pct")))
        ],
        "candidate_details": candidates,
        "research_limits": [
            "The ranking is a two-month historical comparison, not evidence of future alpha.",
            "Six fixed candidate families are compared, so the top-three ranking still has selection bias.",
            "The A-share behavioral model is an OHLCV proxy; the paper used a much larger monthly panel and XGBoost.",
            "The drift-reversal value leg is omitted because point-in-time valuation data are unavailable.",
            "The snapshot universe is the current local monitor universe and therefore has survivorship/selection bias.",
            "No candidate is ranked when average invested exposure is below 20 percent.",
        ],
    }


def attach_robustness_windows(
    result: Dict[str, Any],
    previous_results: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Add fixed, non-overlapping prior windows without changing factor parameters."""
    windows = [*reversed(previous_results), result]
    factor_rows: Dict[str, List[Dict[str, Any]]] = {}
    compact_windows: List[Dict[str, Any]] = []
    for window in windows:
        summaries = window["candidate_summary"]
        compact_windows.append({
            "period": window["period"],
            "current_baseline_return_pct": window["current_baseline"]["total_return_pct"],
            "equal_weight_return_pct": window["equal_weight_buy_hold"]["total_return_pct"],
            "factors": {
                row["factor"]["key"]: {
                    "return_pct": row["metrics"]["total_return_pct"],
                    "max_drawdown_pct": row["metrics"]["max_drawdown_pct"],
                    "turnover_pct": row["metrics"]["turnover_pct"],
                    "average_exposure_pct": row["metrics"]["average_exposure_pct"],
                }
                for row in summaries
            },
        })
        for row in summaries:
            factor_rows.setdefault(row["factor"]["key"], []).append({
                "factor": row["factor"],
                "metrics": row["metrics"],
                "baseline_return": window["current_baseline"]["total_return_pct"],
                "equal_weight_return": window["equal_weight_buy_hold"]["total_return_pct"],
            })

    robustness: List[Dict[str, Any]] = []
    for key, rows in factor_rows.items():
        returns = [_safe_float(row["metrics"].get("total_return_pct")) for row in rows]
        baseline_excess = [
            value - _safe_float(row.get("baseline_return"))
            for value, row in zip(returns, rows)
        ]
        equal_weight_excess = [
            value - _safe_float(row.get("equal_weight_return"))
            for value, row in zip(returns, rows)
        ]
        exposures = [_safe_float(row["metrics"].get("average_exposure_pct")) for row in rows]
        turnovers = [_safe_float(row["metrics"].get("turnover_pct")) for row in rows]
        beat_baseline_count = sum(value > 0 for value in baseline_excess)
        beat_equal_weight_count = sum(value > 0 for value in equal_weight_excess)
        positive_count = sum(value > 0 for value in returns)
        robustness.append({
            "factor": rows[-1]["factor"],
            "window_count": len(rows),
            "window_returns_pct": [round(value, 4) for value in returns],
            "mean_return_pct": round(float(np.mean(returns)), 4),
            "median_return_pct": round(float(np.median(returns)), 4),
            "worst_window_return_pct": round(min(returns), 4),
            "mean_excess_vs_current_pp": round(float(np.mean(baseline_excess)), 4),
            "mean_excess_vs_equal_weight_pp": round(float(np.mean(equal_weight_excess)), 4),
            "beat_current_count": beat_baseline_count,
            "beat_equal_weight_count": beat_equal_weight_count,
            "positive_window_count": positive_count,
            "average_turnover_pct": round(float(np.mean(turnovers)), 4),
            "average_exposure_pct": round(float(np.mean(exposures)), 4),
            "robust_eligible": (
                beat_baseline_count >= max(1, len(rows) - 1)
                and beat_equal_weight_count >= max(1, len(rows) - 1)
                and positive_count >= max(1, len(rows) - 1)
                and float(np.mean(exposures)) >= 20.0
            ),
        })
    robustness.sort(
        key=lambda row: (
            not row["robust_eligible"],
            -_safe_float(row.get("mean_return_pct")),
            -_safe_float(row.get("worst_window_return_pct")),
            _safe_float(row.get("average_turnover_pct")),
        )
    )
    result["robustness_windows"] = compact_windows
    result["robustness_summary"] = robustness
    result["robust_top_three"] = [
        {"rank": rank, **row}
        for rank, row in enumerate(
            [row for row in robustness if row["robust_eligible"]][:3],
            start=1,
        )
    ]
    return result


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--fee-rate", type=float, default=0.0005)
    parser.add_argument("--max-ops", type=int, default=5)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    parser.add_argument("--refresh-snapshot", action="store_true")
    parser.add_argument(
        "--robustness-windows",
        type=int,
        default=3,
        help="Number of non-overlapping prior windows used only for robustness reporting",
    )
    parser.add_argument(
        "--stress-fee-rates",
        default="0.0015,0.003",
        help="Comma-separated one-way all-in fee rates for current-window stress tests",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "reports" / "arxiv_factor_comparison.json",
    )
    args = parser.parse_args()
    end = args.end or datetime.now().date().isoformat()
    start = args.start or (datetime.fromisoformat(end).date() - timedelta(days=args.days)).isoformat()
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] or _configured_a_share_symbols()
    histories, snapshot_info = prepare_history_snapshot(
        symbols=symbols,
        path=args.snapshot,
        period="2y",
        refresh=args.refresh_snapshot,
    )
    result = compare_factors(
        histories=histories,
        symbols=symbols,
        start=start,
        end=end,
        snapshot_info=snapshot_info,
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        max_ops=args.max_ops,
    )
    previous_results: List[Dict[str, Any]] = []
    evaluation_start = datetime.fromisoformat(start).date()
    span_days = max(1, (datetime.fromisoformat(end).date() - evaluation_start).days)
    for _ in range(max(0, args.robustness_windows)):
        prior_end = evaluation_start - timedelta(days=1)
        prior_start = prior_end - timedelta(days=span_days)
        previous_results.append(compare_factors(
            histories=histories,
            symbols=symbols,
            start=prior_start.isoformat(),
            end=prior_end.isoformat(),
            snapshot_info=snapshot_info,
            initial_cash=args.initial_cash,
            fee_rate=args.fee_rate,
            max_ops=args.max_ops,
        ))
        evaluation_start = prior_start
    attach_robustness_windows(result, previous_results)
    stress_rows: List[Dict[str, Any]] = []
    for raw_rate in args.stress_fee_rates.split(","):
        if not raw_rate.strip():
            continue
        stress_rate = float(raw_rate)
        if stress_rate <= 0 or abs(stress_rate - args.fee_rate) < 1e-12:
            continue
        stressed = compare_factors(
            histories=histories,
            symbols=symbols,
            start=start,
            end=end,
            snapshot_info=snapshot_info,
            initial_cash=args.initial_cash,
            fee_rate=stress_rate,
            max_ops=args.max_ops,
        )
        stress_rows.append({
            "fee_rate": stress_rate,
            "current_baseline_return_pct": stressed["current_baseline"]["total_return_pct"],
            "equal_weight_return_pct": stressed["equal_weight_buy_hold"]["total_return_pct"],
            "factor_returns_pct": {
                row["factor"]["key"]: row["metrics"]["total_return_pct"]
                for row in stressed["candidate_summary"]
            },
        })
    result["cost_stress"] = stress_rows
    atomic_write_json(args.out, result)
    print(json.dumps({
        "output": str(args.out),
        "period": result["period"],
        "universe": result["universe"],
        "current_baseline": result["current_baseline"],
        "equal_weight_buy_hold": result["equal_weight_buy_hold"],
        "top_three": result["top_three"],
        "robust_top_three": result["robust_top_three"],
        "cost_stress": result["cost_stress"],
        "candidate_summary": result["candidate_summary"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
