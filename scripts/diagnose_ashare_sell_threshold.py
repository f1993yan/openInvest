"""Diagnose whether A-share sell alerts or exit bands are too strict.

The goal is not to maximize sell frequency.  For each committee-sell band we
measure portfolio utility plus post-sell opportunity cost:

- avoided_drawdown_pct: next-window drawdown avoided after selling.
- missed_rebound_pct: rebound given up after selling.
- net_sell_path_edge_pct: avoided drawdown minus missed rebound.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        return default if math.isnan(out) else out
    except (TypeError, ValueError):
        return default


def _default_dates(days: int) -> tuple[str, str]:
    end = datetime.now().date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _split_symbols(value: str) -> List[str]:
    symbols: List[str] = []
    for raw in value.split(","):
        symbol = raw.strip()
        if not symbol:
            continue
        if symbol.isdigit() and len(symbol) < 6:
            symbol = symbol.zfill(6)
        symbols.append(symbol)
    return symbols


def _sell_reason_counts(trades: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for trade in trades:
        if trade.get("side") != "SELL":
            continue
        reason = str(trade.get("reason") or "未知")
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _post_sell_path_stats(
    trades: List[Dict[str, Any]],
    histories: Dict[str, pd.DataFrame],
    *,
    horizon: int,
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for trade in trades:
        if trade.get("side") != "SELL":
            continue
        symbol = str(trade.get("symbol") or "")
        hist = histories.get(symbol)
        if hist is None or hist.empty:
            continue
        date = pd.to_datetime(trade.get("date"))
        dates = list(hist.index)
        try:
            idx = dates.index(date)
        except ValueError:
            continue
        future = hist.iloc[idx + 1: idx + 1 + horizon]
        if future.empty:
            continue
        price = _safe_float(trade.get("price"))
        if price <= 0:
            continue
        min_low = _safe_float(future["Low"].min(), price)
        max_high = _safe_float(future["High"].max(), price)
        end_close = _safe_float(future["Close"].iloc[-1], price)
        avoided_drawdown = max(0.0, (price - min_low) / price * 100.0)
        missed_rebound = max(0.0, (max_high - price) / price * 100.0)
        forward_return = (end_close / price - 1.0) * 100.0
        rows.append({
            "date": str(trade.get("date")),
            "symbol": symbol,
            "name": trade.get("name"),
            "reason": trade.get("reason"),
            "price": round(price, 4),
            "avoided_drawdown_pct": round(avoided_drawdown, 4),
            "missed_rebound_pct": round(missed_rebound, 4),
            "net_sell_path_edge_pct": round(avoided_drawdown - missed_rebound, 4),
            "forward_return_pct": round(forward_return, 4),
        })
    if not rows:
        return {
            "sample_count": 0,
            "avg_avoided_drawdown_pct": 0.0,
            "avg_missed_rebound_pct": 0.0,
            "avg_net_sell_path_edge_pct": 0.0,
            "positive_edge_rate": 0.0,
            "by_reason": {},
            "samples": [],
        }
    avg_avoided = sum(r["avoided_drawdown_pct"] for r in rows) / len(rows)
    avg_missed = sum(r["missed_rebound_pct"] for r in rows) / len(rows)
    avg_edge = sum(r["net_sell_path_edge_pct"] for r in rows) / len(rows)
    positive = sum(1 for r in rows if r["net_sell_path_edge_pct"] > 0)

    by_reason: Dict[str, Dict[str, Any]] = {}
    for reason in sorted({str(r.get("reason") or "未知") for r in rows}):
        bucket = [r for r in rows if str(r.get("reason") or "未知") == reason]
        if not bucket:
            continue
        reason_positive = sum(1 for r in bucket if r["net_sell_path_edge_pct"] > 0)
        by_reason[reason] = {
            "sample_count": len(bucket),
            "avg_avoided_drawdown_pct": round(
                sum(r["avoided_drawdown_pct"] for r in bucket) / len(bucket),
                4,
            ),
            "avg_missed_rebound_pct": round(
                sum(r["missed_rebound_pct"] for r in bucket) / len(bucket),
                4,
            ),
            "avg_net_sell_path_edge_pct": round(
                sum(r["net_sell_path_edge_pct"] for r in bucket) / len(bucket),
                4,
            ),
            "positive_edge_rate": round(reason_positive / len(bucket), 6),
        }
    return {
        "sample_count": len(rows),
        "avg_avoided_drawdown_pct": round(avg_avoided, 4),
        "avg_missed_rebound_pct": round(avg_missed, 4),
        "avg_net_sell_path_edge_pct": round(avg_edge, 4),
        "positive_edge_rate": round(positive / len(rows), 6),
        "by_reason": by_reason,
        "samples": rows,
    }


def run_diagnosis(
    *,
    symbols: List[str],
    start: str,
    end: str,
    bands: List[float],
    initial_cash: float,
    fee_rate: float,
    max_ops: int,
    horizon: int,
) -> Dict[str, Any]:
    from scripts.backtest_ashare_committee_exit import (
        ExitParams,
        _fetch_histories,
        build_signal_cache,
        run_backtest,
    )

    histories = _fetch_histories(symbols, period="2y")
    names = {symbol: symbol for symbol in symbols}
    signal_cache = build_signal_cache(histories=histories, start=start, end=end)
    baseline_params = ExitParams(
        max_loss_pct=5.0,
        stop_atr_mult=1.6,
        take_profit_r1=1.5,
        take_profit_r2=3.5,
        trailing_atr_mult=3.0,
        min_score_to_buy=60.0,
    )
    band_results: List[Dict[str, Any]] = []
    for band in bands:
        result = run_backtest(
            histories=histories,
            names=names,
            start=start,
            end=end,
            params=baseline_params,
            signal_cache=signal_cache,
            initial_cash=initial_cash,
            fee_rate=fee_rate,
            max_ops_per_symbol_per_day=max_ops,
            committee_sell_stop_band=band,
        )
        path_stats = _post_sell_path_stats(result["trades"], histories, horizon=horizon)
        metrics = dict(result["metrics"])
        utility = _safe_float(metrics.get("policy_quality_score"))
        utility += 0.20 * _safe_float(path_stats.get("avg_net_sell_path_edge_pct"))
        band_results.append({
            "committee_sell_stop_band": round(max(1.0, band), 4),
            "decision_utility": round(utility, 6),
            "metrics": metrics,
            "sell_reason_counts": _sell_reason_counts(result["trades"]),
            "post_sell_path": path_stats,
        })
    band_results.sort(key=lambda row: row["decision_utility"], reverse=True)
    best = band_results[0] if band_results else {}
    return {
        "status": "ok",
        "config": {
            "symbols": symbols,
            "start": start,
            "end": end,
            "bands": bands,
            "initial_cash": initial_cash,
            "fee_rate": fee_rate,
            "max_ops_per_symbol_per_day": max_ops,
            "post_sell_horizon_days": horizon,
        },
        "best": best,
        "band_results": band_results,
        "interpretation": {
            "exit_band_too_strict_if": "looser bands improve decision_utility and avg_net_sell_path_edge_pct without worsening max_drawdown_pct materially",
            "exit_band_not_binding_if": "all bands have nearly identical metrics and sell_reason_counts",
            "sell_alert_layer_suspect_if": "exit backtest is healthy but live rows are suppressed as low_sell_score or shown as candidate/monitoring",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", required=True, help="逗号分隔A股代码")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--bands", default="1.00,1.01,1.03,1.05,1.08,1.10")
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--fee-rate", type=float, default=0.0005)
    parser.add_argument("--max-ops", type=int, default=5)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--out", default=str(ROOT / "reports" / "ashare_sell_threshold_diagnosis.json"))
    args = parser.parse_args()

    start, end = (args.start, args.end) if args.start and args.end else _default_dates(args.days)
    result = run_diagnosis(
        symbols=_split_symbols(args.symbols),
        start=start,
        end=end,
        bands=[_safe_float(x, 1.03) for x in args.bands.split(",") if x.strip()],
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        max_ops=max(0, min(5, args.max_ops)),
        horizon=max(1, args.horizon),
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(out),
        "best": result["best"],
        "summary": [
            {
                "band": row["committee_sell_stop_band"],
                "decision_utility": row["decision_utility"],
                "return_pct": row["metrics"].get("total_return_pct"),
                "max_dd_pct": row["metrics"].get("max_drawdown_pct"),
                "sell_count": row["metrics"].get("sell_count"),
                "sell_win_rate_lower": row["metrics"].get("sell_win_rate_lower"),
                "net_sell_path_edge_pct": row["post_sell_path"].get("avg_net_sell_path_edge_pct"),
                "by_reason": row["post_sell_path"].get("by_reason"),
            }
            for row in result["band_results"]
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
