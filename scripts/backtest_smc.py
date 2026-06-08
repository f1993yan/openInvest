"""Run a deterministic Smart Money Concepts (SMC) backtest.

Examples:
    python -m scripts.backtest_smc 300001 --period 2y
    python -m scripts.backtest_smc 600150 --period 1y --risk 0.5 --json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from core.smc_backtest import SMCBacktestConfig, backtest_smc_strategy
from utils.akshare_data import get_history_data


def run(symbol: str, args: argparse.Namespace) -> dict[str, Any]:
    df = get_history_data(symbol, period=args.period)
    if df is None or df.empty:
        raise SystemExit(f"no OHLCV data for {symbol}")
    cfg = SMCBacktestConfig(
        swing_lookback=args.swing,
        atr_window=args.atr,
        risk_per_trade_pct=args.risk,
        initial_cash=args.cash,
        reward_risk=args.rr,
        max_hold_bars=args.max_hold,
        min_stop_atr=args.min_stop_atr,
        require_fvg=args.require_fvg,
        require_liquidity_sweep=args.require_sweep,
        allow_short=args.allow_short,
        fee_bps=args.fee_bps,
    )
    result = backtest_smc_strategy(df, cfg)
    result["symbol"] = symbol
    result["period"] = args.period
    return result


def print_summary(result: dict[str, Any]) -> None:
    m = result["metrics"]
    print(f"SMC backtest: {result['symbol']} ({result['period']})")
    print(
        "return={total_return_pct:.2f}%  maxDD={max_drawdown_pct:.2f}%  "
        "sharpe={sharpe_ratio:.2f}  trades={trade_count}  win={win_rate_pct:.1f}%  "
        "profit_factor={profit_factor}".format(**m)
    )
    print("\nRecent trades:")
    for t in result["trades"][-10:]:
        print(
            f"- {t['entry_date']} -> {t['exit_date']} {t['direction']} "
            f"{t['entry_price']} -> {t['exit_price']} "
            f"pnl={t['pnl']} ({t['return_pct']}%) {t['exit_reason']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="SMC concept strategy backtest")
    parser.add_argument("symbol", help="A-share symbol, e.g. 300001 or 600150")
    parser.add_argument("--period", default="2y", help="history period: 3mo/6mo/1y/2y")
    parser.add_argument("--cash", type=float, default=100_000.0, help="initial cash")
    parser.add_argument("--risk", type=float, default=1.0, help="risk per trade percent")
    parser.add_argument("--rr", type=float, default=2.0, help="reward/risk target")
    parser.add_argument("--swing", type=int, default=3, help="swing pivot lookback")
    parser.add_argument("--atr", type=int, default=14, help="ATR window")
    parser.add_argument("--max-hold", type=int, default=20, help="maximum holding bars")
    parser.add_argument("--min-stop-atr", type=float, default=0.8, help="minimum stop distance in ATR")
    parser.add_argument("--fee-bps", type=float, default=5.0, help="one-way fee in basis points")
    parser.add_argument("--require-fvg", action="store_true", help="enter only when BOS also has FVG")
    parser.add_argument("--require-sweep", action="store_true", help="enter only when BOS also has liquidity sweep")
    parser.add_argument("--allow-short", action="store_true", help="allow short trades for research markets that support shorting")
    parser.add_argument("--json", action="store_true", help="print JSON result")
    parser.add_argument("--output", help="write JSON result to a file")
    args = parser.parse_args()

    result = run(args.symbol, args)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_summary(result)


if __name__ == "__main__":
    main()
