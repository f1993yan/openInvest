"""Two-month old/new comparison for the July data-integrity upgrade.

Only the historical-pricing change is allowed to affect simulated PnL.  API
auth, backups, decision ids, privacy suppression, and the review-only intraday
sentinel are explicitly profit-neutral in this comparison.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_ashare_committee_exit import (  # noqa: E402
    ExitParams,
    _fetch_histories,
    build_signal_cache,
    run_backtest,
)
from utils.safe_persistence import atomic_write_json  # noqa: E402


SNAPSHOT_SCHEMA_VERSION = 1
HISTORY_COLUMNS = ("Open", "High", "Low", "Close", "Volume", "PrevClose")
DEFAULT_SNAPSHOT_PATH = ROOT / "reports" / "upgrade_backtest_market_snapshot.json"


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def production_exit_params() -> ExitParams:
    return ExitParams(
        max_loss_pct=_env_float("INVEST_A_SHARE_POSITION_MAX_LOSS_PCT", 8.0),
        stop_atr_mult=_env_float("INVEST_A_SHARE_POSITION_STOP_ATR_MULT", 2.0),
        take_profit_r1=_env_float("INVEST_A_SHARE_POSITION_TAKE_PROFIT_R1", 1.5),
        take_profit_r2=_env_float("INVEST_A_SHARE_POSITION_TAKE_PROFIT_R2", 2.5),
        trailing_atr_mult=_env_float("INVEST_A_SHARE_POSITION_TRAIL_ATR_MULT", 2.8),
        min_score_to_buy=_env_float("INVEST_A_SHARE_MIN_SCORE_TO_BUY", 60.0),
    )


def _configured_a_share_symbols() -> List[str]:
    path = ROOT / "jobs" / "market_monitor_config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    symbols = []
    for stock in list(config.get("holdings") or []) + list(config.get("watchlist") or []):
        symbol = str(stock.get("symbol") or "").strip().upper()
        market = str(stock.get("market") or "a").strip().lower()
        if symbol.isdigit() and len(symbol) == 6 and market in {"a", "ashare", "cn", "sh", "sz"}:
            if symbol not in symbols:
                symbols.append(symbol)
    if not symbols:
        raise ValueError("monitor config contains no A-share symbols")
    return symbols


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _history_rows(frame: Any) -> List[List[Any]]:
    missing = [column for column in HISTORY_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"history is missing columns: {', '.join(missing)}")
    rows: List[List[Any]] = []
    for index, row in frame.sort_index().iterrows():
        values = [float(row[column]) for column in HISTORY_COLUMNS]
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"history contains a non-finite value at {index}")
        rows.append([index.strftime("%Y-%m-%d"), *values])
    if not rows:
        raise ValueError("history is empty")
    return rows


def history_manifest(histories: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deterministic digest and range summary for normalized bars."""
    payload: Dict[str, List[List[Any]]] = {}
    symbols: Dict[str, Dict[str, Any]] = {}
    for symbol in sorted(histories):
        rows = _history_rows(histories[symbol])
        payload[symbol] = rows
        symbols[symbol] = {
            "sha256": hashlib.sha256(_canonical_json_bytes(rows)).hexdigest(),
            "row_count": len(rows),
            "first_date": rows[0][0],
            "last_date": rows[-1][0],
        }
    return {
        "sha256": hashlib.sha256(_canonical_json_bytes(payload)).hexdigest(),
        "symbol_count": len(symbols),
        "symbols": symbols,
    }


def _clone_histories(histories: Dict[str, Any]) -> Dict[str, Any]:
    return {symbol: frame.copy(deep=True) for symbol, frame in histories.items()}


def write_history_snapshot(
    path: Path,
    histories: Dict[str, Any],
    *,
    period: str,
) -> Dict[str, Any]:
    """Persist one immutable market-only input set for repeatable comparisons."""
    manifest = history_manifest(histories)
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "period": period,
        "columns": list(HISTORY_COLUMNS),
        "manifest": manifest,
        "histories": {
            symbol: _history_rows(histories[symbol])
            for symbol in sorted(histories)
        },
    }
    atomic_write_json(path, payload)
    return payload


def load_history_snapshot(
    path: Path,
    *,
    expected_symbols: Sequence[str] | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Load a frozen input set and reject schema, universe, or hash drift."""
    import pandas as pd

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("unsupported history snapshot schema")
    if payload.get("columns") != list(HISTORY_COLUMNS):
        raise ValueError("history snapshot columns do not match the current schema")
    raw_histories = payload.get("histories")
    if not isinstance(raw_histories, dict) or not raw_histories:
        raise ValueError("history snapshot contains no symbols")
    if expected_symbols is not None and set(raw_histories) != set(expected_symbols):
        raise ValueError(
            "history snapshot universe differs from the requested universe; "
            "run again with --refresh-snapshot"
        )

    histories: Dict[str, Any] = {}
    for symbol, rows in raw_histories.items():
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"history snapshot is empty for {symbol}")
        frame = pd.DataFrame(rows, columns=("Date", *HISTORY_COLUMNS))
        frame["Date"] = pd.to_datetime(frame["Date"], errors="raise")
        for column in HISTORY_COLUMNS:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        frame = frame.set_index("Date").sort_index()
        if not frame.index.is_unique:
            raise ValueError(f"history snapshot has duplicate dates for {symbol}")
        histories[str(symbol)] = frame

    actual = history_manifest(histories)
    expected = payload.get("manifest") or {}
    if actual.get("sha256") != expected.get("sha256"):
        raise ValueError("history snapshot SHA-256 validation failed")
    if actual.get("symbols") != expected.get("symbols"):
        raise ValueError("history snapshot per-symbol manifest validation failed")
    return histories, payload


def prepare_history_snapshot(
    *,
    symbols: Sequence[str],
    path: Path,
    period: str = "2y",
    refresh: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Reuse a verified snapshot unless the caller explicitly refreshes it."""
    snapshot_path = Path(path)
    if snapshot_path.exists() and not refresh:
        histories, payload = load_history_snapshot(
            snapshot_path,
            expected_symbols=symbols,
        )
        source = "reused"
    else:
        histories = _fetch_histories(symbols, period=period)
        payload = write_history_snapshot(snapshot_path, histories, period=period)
        histories, payload = load_history_snapshot(
            snapshot_path,
            expected_symbols=symbols,
        )
        source = "refreshed" if refresh else "created"
    snapshot_info = {
        "schema_version": payload["schema_version"],
        "captured_at": payload["captured_at"],
        "period": payload["period"],
        "source": source,
        "path": str(snapshot_path.relative_to(ROOT)) if snapshot_path.is_relative_to(ROOT) else snapshot_path.name,
        **payload["manifest"],
    }
    return histories, snapshot_info


def _daily_log_returns(equity_curve: Sequence[Dict[str, Any]]) -> List[float]:
    values = [float(row.get("equity") or 0.0) for row in equity_curve]
    return [
        math.log(current / previous)
        for previous, current in zip(values, values[1:])
        if previous > 0 and current > 0
    ]


def paired_block_bootstrap_return_difference(
    old_curve: Sequence[Dict[str, Any]],
    new_curve: Sequence[Dict[str, Any]],
    *,
    block_size: int = 5,
    samples: int = 4000,
    seed: int = 20260712,
) -> Dict[str, Any]:
    old_returns = _daily_log_returns(old_curve)
    new_returns = _daily_log_returns(new_curve)
    n = min(len(old_returns), len(new_returns))
    if n < max(20, block_size * 2):
        return {"available": False, "reason": "fewer_than_20_paired_trading_days", "paired_days": n}
    pairs = list(zip(old_returns[:n], new_returns[:n]))
    starts = list(range(0, n - block_size + 1))
    rng = random.Random(seed)
    differences = []
    for _ in range(max(200, int(samples))):
        sampled: List[Tuple[float, float]] = []
        while len(sampled) < n:
            start = rng.choice(starts)
            sampled.extend(pairs[start:start + block_size])
        sampled = sampled[:n]
        old_total = math.exp(sum(item[0] for item in sampled)) - 1.0
        new_total = math.exp(sum(item[1] for item in sampled)) - 1.0
        differences.append((new_total - old_total) * 100.0)
    differences.sort()

    def quantile(q: float) -> float:
        position = (len(differences) - 1) * q
        low = math.floor(position)
        high = math.ceil(position)
        if low == high:
            return differences[low]
        weight = position - low
        return differences[low] * (1.0 - weight) + differences[high] * weight

    return {
        "available": True,
        "paired_days": n,
        "block_size": block_size,
        "bootstrap_samples": max(200, int(samples)),
        "mean_difference_pp": round(statistics.mean(differences), 6),
        "confidence_interval_95_pp": [round(quantile(0.025), 6), round(quantile(0.975), 6)],
    }


def compare(
    *,
    symbols: List[str],
    start: str,
    end: str,
    initial_cash: float = 100_000.0,
    fee_rate: float = 0.0005,
    max_ops: int = 5,
    histories: Dict[str, Any] | None = None,
    history_snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    frozen_histories = _clone_histories(histories or _fetch_histories(symbols, period="2y"))
    input_manifest = history_manifest(frozen_histories)
    signal_cache = build_signal_cache(histories=frozen_histories, start=start, end=end)
    if history_manifest(frozen_histories)["sha256"] != input_manifest["sha256"]:
        raise RuntimeError("signal construction modified the frozen history input")
    params = production_exit_params()
    common = dict(
        names={symbol: symbol for symbol in symbols},
        start=start,
        end=end,
        params=params,
        signal_cache=signal_cache,
        initial_cash=initial_cash,
        fee_rate=fee_rate,
        max_ops_per_symbol_per_day=max_ops,
        strategy_mode="legacy",
    )
    old_histories = _clone_histories(frozen_histories)
    old = run_backtest(
        **common,
        histories=old_histories,
        missing_price_policy="legacy_avg_cost",
    )
    old_input_unchanged = history_manifest(old_histories)["sha256"] == input_manifest["sha256"]
    new_histories = _clone_histories(frozen_histories)
    new = run_backtest(
        **common,
        histories=new_histories,
        missing_price_policy="carry_forward",
    )
    new_input_unchanged = history_manifest(new_histories)["sha256"] == input_manifest["sha256"]
    if not old_input_unchanged or not new_input_unchanged:
        raise RuntimeError("backtest modified the frozen history input")
    old_metrics = old["metrics"]
    new_metrics = new["metrics"]
    metric_keys = (
        "final_equity",
        "total_return_pct",
        "max_drawdown_pct",
        "return_risk_ratio",
        "sortino_ratio",
        "downside_deviation_pct",
        "turnover_pct",
        "total_fees_cny",
        "trade_count",
    )
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "period": {"start": start, "end": end},
        "universe": {"market": "A-share", "symbol_count": len(symbols)},
        "history_snapshot": {
            **(history_snapshot or {}),
            "sha256": input_manifest["sha256"],
            "snapshot_id": input_manifest["sha256"][:16],
            "symbol_count": input_manifest["symbol_count"],
            "symbols": input_manifest["symbols"],
            "signal_build_input_unchanged": True,
            "old_backtest_input_unchanged": old_input_unchanged,
            "new_backtest_input_unchanged": new_input_unchanged,
        },
        "constraints": {
            "initial_cash_cny": initial_cash,
            "fee_rate": fee_rate,
            "max_operations_per_symbol_per_day": max_ops,
            "a_share_t_plus_1": True,
        },
        "old": {key: old_metrics.get(key) for key in metric_keys},
        "new": {key: new_metrics.get(key) for key in metric_keys},
        "difference_new_minus_old": {
            key: round(float(new_metrics.get(key) or 0.0) - float(old_metrics.get(key) or 0.0), 6)
            for key in metric_keys
        },
        "paired_block_bootstrap": paired_block_bootstrap_return_difference(
            old["equity_curve"],
            new["equity_curve"],
        ),
        "data_quality": {
            "old_missing_held_symbol_days": old["diagnostics"].get("missing_held_symbol_days", 0),
            "new_missing_held_symbol_days": new["diagnostics"].get("missing_held_symbol_days", 0),
            "new_valuation_policy": "last valid close carried forward; no fabricated fill and no trade on missing bars",
        },
        "profit_attribution": {
            "simulated": ["missing held-bar valuation: avg cost -> last valid close"],
            "expected_zero_direct_return_effect": [
                "fixed 30d labels and as-of ATR",
                "decision_id and idempotent ledger",
                "API auth/CORS",
                "backup/restore and WAL-consistent exports",
                "adjustment splice detection when no splice is present",
            ],
            "not_mechanically_traded": [
                "99.5% empirical intraday sentinel; it only requests committee review",
            ],
        },
        "limitations": [
            "The comparison uses deterministic committee-style signals, not retrospective LLM calls.",
            "The same production exit parameters and signals are used on both sides.",
            "The frozen snapshot is reused until --refresh-snapshot is explicitly supplied.",
            "Bootstrap intervals measure paired daily path uncertainty, not future-return guarantees.",
        ],
    }


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="", help="Optional comma-separated A-share symbols")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--fee-rate", type=float, default=0.0005)
    parser.add_argument("--max-ops", type=int, default=5)
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT_PATH,
        help="Frozen market-only input snapshot (reused by default)",
    )
    parser.add_argument(
        "--refresh-snapshot",
        action="store_true",
        help="Explicitly replace the frozen history snapshot before comparing",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "reports" / "upgrade_old_new_two_month_comparison.json"),
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
    result = compare(
        symbols=symbols,
        start=start,
        end=end,
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        max_ops=args.max_ops,
        histories=histories,
        history_snapshot=snapshot_info,
    )
    out = Path(args.out)
    atomic_write_json(out, result)
    print(json.dumps({"output": str(out), **{key: result[key] for key in ("period", "universe", "history_snapshot", "old", "new", "difference_new_minus_old", "paired_block_bootstrap")}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
