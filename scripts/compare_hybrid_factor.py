"""Combine spatio-temporal momentum with the A-share behavioral factor.

Variant selection uses only three non-overlapping windows before the requested
two-month evaluation window.  The current window is kept as a strict holdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.compare_arxiv_factors import (  # noqa: E402
    FACTOR_SPECS,
    FactorSpec,
    _load_sector_mapping,
    compare_factors,
    run_factor_backtest,
)
from scripts.compare_upgrade_profitability import (  # noqa: E402
    DEFAULT_SNAPSHOT_PATH,
    _configured_a_share_symbols,
    paired_block_bootstrap_return_difference,
    prepare_history_snapshot,
)
from utils.safe_persistence import atomic_write_json  # noqa: E402


HYBRID_VARIANTS: Sequence[FactorSpec] = (
    FactorSpec("hybrid_w50_r10_b50", "混合50/50-10日缓动", 10, 4, spatio_weight=0.50, target_blend=0.50, min_trade_weight=0.05),
    FactorSpec("hybrid_w60_r10_b50", "混合60/40-10日缓动", 10, 4, spatio_weight=0.60, target_blend=0.50, min_trade_weight=0.05),
    FactorSpec("hybrid_w75_r10_b50", "混合75/25-10日缓动", 10, 4, spatio_weight=0.75, target_blend=0.50, min_trade_weight=0.05),
    FactorSpec("hybrid_w50_r15_b50", "混合50/50-15日缓动", 15, 4, spatio_weight=0.50, target_blend=0.50, min_trade_weight=0.03),
    FactorSpec("hybrid_w60_r20_raw", "混合60/40-20日", 20, 4, spatio_weight=0.60, target_blend=1.00, min_trade_weight=0.05),
    FactorSpec("hybrid_w70_r20_b50", "混合70/30-20日缓动", 20, 4, spatio_weight=0.70, target_blend=0.50, min_trade_weight=0.05),
)


def _spec(key: str) -> FactorSpec:
    return next(item for item in FACTOR_SPECS if item.key == key)


def _prior_windows(
    start: str,
    end: str,
    count: int = 3,
    *,
    span_days: int | None = None,
) -> List[Dict[str, str]]:
    evaluation_start = datetime.fromisoformat(start).date()
    window_span = max(
        1,
        int(span_days)
        if span_days is not None
        else (datetime.fromisoformat(end).date() - evaluation_start).days,
    )
    windows: List[Dict[str, str]] = []
    for _ in range(count):
        prior_end = evaluation_start - timedelta(days=1)
        prior_start = prior_end - timedelta(days=window_span)
        windows.append({"start": prior_start.isoformat(), "end": prior_end.isoformat()})
        evaluation_start = prior_start
    return list(reversed(windows))


def _resolve_trading_window(
    histories: Dict[str, Any],
    *,
    end: str,
    trading_days: int,
) -> tuple[str, str]:
    end_ts = pd.Timestamp(end)
    dates = sorted({date for frame in histories.values() for date in frame.index if date <= end_ts})
    count = max(1, int(trading_days))
    if len(dates) < count:
        raise ValueError(f"history has only {len(dates)} trading days before {end}; requested {count}")
    selected = dates[-count:]
    return selected[0].strftime("%Y-%m-%d"), selected[-1].strftime("%Y-%m-%d")


def _aggregate(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    returns = [float(row["metrics"]["total_return_pct"]) for row in rows]
    turnovers = [float(row["metrics"]["turnover_pct"]) for row in rows]
    drawdowns = [float(row["metrics"]["max_drawdown_pct"]) for row in rows]
    return {
        "window_returns_pct": [round(value, 4) for value in returns],
        "mean_return_pct": round(float(np.mean(returns)), 4),
        "median_return_pct": round(float(np.median(returns)), 4),
        "worst_return_pct": round(min(returns), 4),
        "positive_window_count": sum(value > 0 for value in returns),
        "mean_turnover_pct": round(float(np.mean(turnovers)), 4),
        "mean_max_drawdown_pct": round(float(np.mean(drawdowns)), 4),
    }


def compare_hybrids(
    *,
    histories: Dict[str, Any],
    symbols: List[str],
    start: str,
    end: str,
    snapshot_info: Dict[str, Any],
    initial_cash: float,
    fee_rate: float,
    training_window_calendar_days: int = 60,
) -> Dict[str, Any]:
    sectors = _load_sector_mapping(symbols)
    prior_windows = _prior_windows(
        start,
        end,
        3,
        span_days=training_window_calendar_days,
    )
    holdout_dates = sorted({
        date
        for frame in histories.values()
        for date in frame.index
        if pd.Timestamp(start) <= date <= pd.Timestamp(end)
    })
    constituent_specs = (_spec("spatio_temporal"), _spec("a_share_behavioral"))
    all_specs = (*constituent_specs, *HYBRID_VARIANTS)
    training_runs: Dict[str, List[Dict[str, Any]]] = {spec.key: [] for spec in all_specs}
    for window in prior_windows:
        for spec in all_specs:
            training_runs[spec.key].append(run_factor_backtest(
                spec=spec,
                histories=histories,
                start=window["start"],
                end=window["end"],
                sectors=sectors,
                initial_cash=initial_cash,
                fee_rate=fee_rate,
            ))
    training_summary = {
        key: _aggregate(rows)
        for key, rows in training_runs.items()
    }
    strict_turnover_budget = min(
        training_summary["spatio_temporal"]["mean_turnover_pct"],
        training_summary["a_share_behavioral"]["mean_turnover_pct"],
    )
    relaxed_turnover_budget = training_summary["spatio_temporal"]["mean_turnover_pct"]
    eligible = [
        spec for spec in HYBRID_VARIANTS
        if training_summary[spec.key]["positive_window_count"] >= 2
        and training_summary[spec.key]["mean_turnover_pct"] <= strict_turnover_budget
    ]
    selection_rule = "maximize prior mean return under turnover <= lower-turnover constituent"
    if not eligible:
        eligible = [
            spec for spec in HYBRID_VARIANTS
            if training_summary[spec.key]["positive_window_count"] >= 2
            and training_summary[spec.key]["mean_turnover_pct"] <= relaxed_turnover_budget
        ]
        selection_rule = "maximize prior mean return under turnover <= spatio-temporal constituent"
    if not eligible:
        eligible = list(HYBRID_VARIANTS)
        selection_rule = "fallback: maximize prior mean return"
    chosen = max(
        eligible,
        key=lambda spec: (
            training_summary[spec.key]["mean_return_pct"],
            training_summary[spec.key]["worst_return_pct"],
            -training_summary[spec.key]["mean_turnover_pct"],
        ),
    )

    current_context = compare_factors(
        histories=histories,
        symbols=symbols,
        start=start,
        end=end,
        snapshot_info=snapshot_info,
        initial_cash=initial_cash,
        fee_rate=fee_rate,
    )
    current_runs = {
        spec.key: run_factor_backtest(
            spec=spec,
            histories=histories,
            start=start,
            end=end,
            sectors=sectors,
            initial_cash=initial_cash,
            fee_rate=fee_rate,
        )
        for spec in HYBRID_VARIANTS
    }
    spatio = next(
        row for row in current_context["candidate_details"]
        if row["factor"]["key"] == "spatio_temporal"
    )
    behavioral = next(
        row for row in current_context["candidate_details"]
        if row["factor"]["key"] == "a_share_behavioral"
    )
    current_summary: List[Dict[str, Any]] = []
    for spec in HYBRID_VARIANTS:
        run = current_runs[spec.key]
        metrics = run["metrics"]
        current_summary.append({
            "factor": run["factor"],
            "metrics": metrics,
            "return_vs_spatio_pp": round(
                float(metrics["total_return_pct"]) - float(spatio["metrics"]["total_return_pct"]),
                4,
            ),
            "turnover_vs_spatio_pp": round(
                float(metrics["turnover_pct"]) - float(spatio["metrics"]["turnover_pct"]),
                4,
            ),
            "return_vs_behavioral_pp": round(
                float(metrics["total_return_pct"]) - float(behavioral["metrics"]["total_return_pct"]),
                4,
            ),
            "turnover_vs_behavioral_pp": round(
                float(metrics["turnover_pct"]) - float(behavioral["metrics"]["turnover_pct"]),
                4,
            ),
            "simultaneously_beats_spatio": (
                float(metrics["total_return_pct"]) > float(spatio["metrics"]["total_return_pct"])
                and float(metrics["turnover_pct"]) < float(spatio["metrics"]["turnover_pct"])
            ),
            "bootstrap_vs_spatio": paired_block_bootstrap_return_difference(
                spatio["equity_curve"],
                run["equity_curve"],
            ),
        })
    current_summary.sort(key=lambda row: -float(row["metrics"]["total_return_pct"]))
    selected_current = next(row for row in current_summary if row["factor"]["key"] == chosen.key)
    simultaneous = [row for row in current_summary if row["simultaneously_beats_spatio"]]
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "period": {"start": start, "end": end},
        "history_snapshot": snapshot_info,
        "constraints": {
            "initial_cash_cny": initial_cash,
            "fee_rate": fee_rate,
            "training_uses_current_window": False,
            "a_share_t_plus_1": True,
            "integer_lots": True,
            "limit_up_down": True,
            "holdout_trading_days": len(holdout_dates),
            "training_window_calendar_days": training_window_calendar_days,
        },
        "training_windows": prior_windows,
        "selection": {
            "rule": selection_rule,
            "strict_turnover_budget_pct": strict_turnover_budget,
            "relaxed_turnover_budget_pct": relaxed_turnover_budget,
            "selected_key": chosen.key,
            "selected_training_metrics": training_summary[chosen.key],
        },
        "training_summary": training_summary,
        "current_baselines": {
            "committee": current_context["current_baseline"],
            "equal_weight": current_context["equal_weight_buy_hold"],
            "spatio_temporal": spatio["metrics"],
            "a_share_behavioral": behavioral["metrics"],
        },
        "selected_holdout_result": selected_current,
        "simultaneous_improvement_count": len(simultaneous),
        "simultaneous_improvements": simultaneous,
        "all_holdout_variants": current_summary,
        "limits": [
            "Six predeclared hybrid variants are selected on three prior windows, so model-selection risk remains.",
            "The current monitor universe is known today and retains survivorship/selection bias.",
            "A hybrid is called simultaneous improvement only if holdout return is higher and turnover lower than spatio-temporal momentum.",
        ],
    }


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
    parser.add_argument(
        "--trading-days",
        type=int,
        default=0,
        help="Use exactly this many market trading days; overrides --days and --start",
    )
    parser.add_argument("--training-window-calendar-days", type=int, default=60)
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--fee-rate", type=float, default=0.0005)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "reports" / "hybrid_factor_comparison.json",
    )
    args = parser.parse_args()
    requested_end = args.end or datetime.now().date().isoformat()
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] or _configured_a_share_symbols()
    histories, snapshot_info = prepare_history_snapshot(
        symbols=symbols,
        path=args.snapshot,
        period="2y",
        refresh=False,
    )
    if args.trading_days > 0:
        start, end = _resolve_trading_window(
            histories,
            end=requested_end,
            trading_days=args.trading_days,
        )
    else:
        end = requested_end
        start = args.start or (datetime.fromisoformat(end).date() - timedelta(days=args.days)).isoformat()
    result = compare_hybrids(
        histories=histories,
        symbols=symbols,
        start=start,
        end=end,
        snapshot_info=snapshot_info,
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        training_window_calendar_days=args.training_window_calendar_days,
    )
    atomic_write_json(args.out, result)
    print(json.dumps({
        "output": str(args.out),
        "period": result["period"],
        "selection": result["selection"],
        "current_baselines": result["current_baselines"],
        "selected_holdout_result": result["selected_holdout_result"],
        "simultaneous_improvement_count": result["simultaneous_improvement_count"],
        "simultaneous_improvements": result["simultaneous_improvements"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
