"""Weekly sector-aware discrete optimization for A-share exit parameters."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "jobs" / "market_monitor_config.json"
REPORT_PATH = ROOT / "reports" / "weekly_exit_param_optimization.json"

EASTMONEY_INDUSTRY_FALLBACK: Dict[str, str] = {}


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path, override=False)
        return
    except Exception:
        pass
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "off", "no", "n"}


def _load_monitor_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _sector_key(stock: Dict[str, Any], sector_by_symbol: Dict[str, str]) -> str:
    symbol = str(stock.get("symbol") or "").strip()
    eastmoney_sector = sector_by_symbol.get(symbol)
    return str(eastmoney_sector or stock.get("sector") or stock.get("industry") or "未分组").strip() or "未分组"


def _sector_symbol_groups(
    config: Dict[str, Any],
    max_symbols_per_sector: int,
    sector_by_symbol: Dict[str, str],
) -> Dict[str, List[Dict[str, str]]]:
    groups: Dict[str, List[Dict[str, str]]] = {}
    for stock in list(config.get("holdings") or []) + list(config.get("watchlist") or []):
        if str(stock.get("market", "a")).lower() != "a":
            continue
        symbol = str(stock.get("symbol") or "").strip()
        if not symbol:
            continue
        sector = _sector_key(stock, sector_by_symbol)
        bucket = groups.setdefault(sector, [])
        if len(bucket) >= max_symbols_per_sector:
            continue
        if any(row["symbol"] == symbol for row in bucket):
            continue
        bucket.append({
            "symbol": symbol,
            "name": str(stock.get("name") or symbol),
            "sector": sector,
            "industry": str(stock.get("industry") or ""),
        })
    return groups


def _fallback_symbols() -> Dict[str, List[Dict[str, str]]]:
    symbols = [s.strip() for s in os.getenv("INVEST_EXIT_PARAM_OPT_SYMBOLS", "").split(",") if s.strip()]
    return {
        "全局": [{"symbol": symbol, "name": symbol, "sector": "全局", "industry": ""} for symbol in symbols]
    } if symbols else {}


def _target_a_share_symbols(config: Dict[str, Any]) -> List[str]:
    symbols: List[str] = []
    for stock in list(config.get("holdings") or []) + list(config.get("watchlist") or []):
        if str(stock.get("market", "a")).lower() != "a":
            continue
        symbol = str(stock.get("symbol") or "").strip()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _fetch_eastmoney_industry_map(symbols: List[str]) -> Tuple[Dict[str, str], str, List[str]]:
    """Map A-share symbols to Eastmoney industry-board names.

    The live path uses Eastmoney industry boards and their constituents.
    Fallback sector mappings are intentionally not shipped in the public repo;
    local users should provide sectors in market_monitor_config.json or rerun
    when Eastmoney is reachable.
    """
    target = set(symbols)
    if not target:
        return {}, "empty", []
    try:
        import akshare as ak  # type: ignore

        board_df = ak.stock_board_industry_name_em()
        mapping: Dict[str, str] = {}
        failures: List[str] = []
        for _, row in board_df.iterrows():
            sector = str(row.get("板块名称") or "").strip()
            code = str(row.get("板块代码") or "").strip()
            if not sector or not code:
                continue
            try:
                cons = ak.stock_board_industry_cons_em(symbol=code)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{sector}:{type(exc).__name__}")
                continue
            for _, item in cons.iterrows():
                symbol = str(item.get("代码") or "").strip()
                if symbol in target and symbol not in mapping:
                    mapping[symbol] = sector
            if target.issubset(mapping):
                break
            time.sleep(0.05)
        missing = sorted(target - set(mapping))
        if mapping:
            if missing:
                for symbol in missing:
                    if symbol in EASTMONEY_INDUSTRY_FALLBACK:
                        mapping[symbol] = EASTMONEY_INDUSTRY_FALLBACK[symbol]
                source = "eastmoney_live_with_fallback"
            else:
                source = "eastmoney_live"
            return mapping, source, failures[:10]
    except Exception as exc:  # noqa: BLE001
        return (
            {symbol: EASTMONEY_INDUSTRY_FALLBACK[symbol] for symbol in symbols if symbol in EASTMONEY_INDUSTRY_FALLBACK},
            f"eastmoney_fallback_after_{type(exc).__name__}",
            [str(exc)[:200]],
        )
    return (
        {symbol: EASTMONEY_INDUSTRY_FALLBACK[symbol] for symbol in symbols if symbol in EASTMONEY_INDUSTRY_FALLBACK},
        "eastmoney_fallback",
        [],
    )


def _representative_values(values: List[float], *, limit: int) -> List[float]:
    clean = sorted({float(v) for v in values})
    if len(clean) <= limit:
        return clean
    if limit <= 1:
        return [clean[len(clean) // 2]]
    indexes = {
        round(i * (len(clean) - 1) / (limit - 1))
        for i in range(limit)
    }
    return [clean[i] for i in sorted(indexes)]


def _cap_param_grid(param_grid: List[Any], max_trials: int) -> List[Any]:
    """Keep the discrete optimization finite enough for weekly unattended runs.

    The full Cartesian product can explode on volatile sectors.  We retain the
    same argmax objective, but evaluate representative low/mid/high quantile
    candidates for each parameter dimension, then truncate deterministically.
    """
    if max_trials <= 0 or len(param_grid) <= max_trials:
        return param_grid
    fields = [
        "max_loss_pct",
        "stop_atr_mult",
        "take_profit_r1",
        "take_profit_r2",
        "trailing_atr_mult",
        "min_score_to_buy",
    ]
    value_sets = {
        field: _representative_values([getattr(p, field) for p in param_grid], limit=3)
        for field in fields
    }
    by_key = {tuple(getattr(p, field) for field in fields): p for p in param_grid}
    selected: List[Any] = []
    seen = set()
    for values in product(*(value_sets[field] for field in fields)):
        if values in seen:
            continue
        item = by_key.get(values)
        if item is None:
            continue
        seen.add(values)
        selected.append(item)
        if len(selected) >= max_trials:
            return selected
    for item in param_grid:
        key = tuple(getattr(item, field) for field in fields)
        if key in seen:
            continue
        selected.append(item)
        if len(selected) >= max_trials:
            break
    return selected


def run() -> Dict[str, Any]:
    _load_dotenv_file(ROOT / ".env")
    from scripts.backtest_ashare_committee_exit import (
        _default_dates,
        _fetch_histories,
        build_discrete_param_grid,
        build_signal_cache,
        optimize_exit_params,
        update_env_exit_params,
        update_env_sector_exit_policies,
    )

    days = max(20, int(os.getenv("INVEST_EXIT_PARAM_OPT_DAYS", "31")))
    initial_cash = float(os.getenv("INVEST_EXIT_PARAM_OPT_INITIAL_CASH", "100000"))
    fee_rate = float(os.getenv("INVEST_EXIT_PARAM_OPT_FEE_RATE", "0.0005"))
    max_ops = max(0, min(5, int(os.getenv("INVEST_EXIT_PARAM_OPT_MAX_OPS", "5"))))
    max_symbols_per_sector = max(1, min(3, int(os.getenv("INVEST_EXIT_PARAM_OPT_MAX_SYMBOLS_PER_SECTOR", "3"))))
    max_trials_per_sector = max(27, int(os.getenv("INVEST_EXIT_PARAM_OPT_MAX_TRIALS_PER_SECTOR", "243")))
    history_period = os.getenv("INVEST_EXIT_PARAM_OPT_HISTORY_PERIOD", "2y")
    start, end = _default_dates(days)

    config = _load_monitor_config()
    target_symbols = _target_a_share_symbols(config)
    sector_by_symbol, sector_source, sector_warnings = _fetch_eastmoney_industry_map(target_symbols)
    sector_groups = _sector_symbol_groups(config, max_symbols_per_sector, sector_by_symbol) or _fallback_symbols()
    only_sectors = {
        s.strip()
        for s in os.getenv("INVEST_EXIT_PARAM_OPT_SECTORS", "").split(",")
        if s.strip()
    }
    if only_sectors:
        sector_groups = {k: v for k, v in sector_groups.items() if k in only_sectors}
    sector_results: Dict[str, Any] = {}
    sector_policies: Dict[str, Dict[str, Any]] = {}

    for sector, stocks in sorted(sector_groups.items()):
        symbols = [row["symbol"] for row in stocks]
        names = {row["symbol"]: row["name"] for row in stocks}
        try:
            histories = _fetch_histories(symbols, period=history_period)
            signal_cache = build_signal_cache(histories=histories, start=start, end=end)
            param_grid, grid_diagnostics = build_discrete_param_grid(histories, start=start, end=end)
            original_trial_count = len(param_grid)
            param_grid = _cap_param_grid(param_grid, max_trials_per_sector)
            grid_diagnostics["original_theta_count"] = original_trial_count
            grid_diagnostics["evaluated_theta_count"] = len(param_grid)
            grid_diagnostics["max_trials_per_sector"] = max_trials_per_sector
            result = optimize_exit_params(
                histories=histories,
                names=names,
                start=start,
                end=end,
                signal_cache=signal_cache,
                initial_cash=initial_cash,
                fee_rate=fee_rate,
                max_ops_per_symbol_per_day=max_ops,
                param_grid=param_grid,
            )
        except Exception as exc:  # noqa: BLE001
            sector_results[sector] = {
                "symbols": names,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            continue
        best_params = dict(result["best"]["params"])
        best_metrics = dict(result["best"].get("metrics") or {})
        metric_keys = (
            "objective_score",
            "policy_quality_score",
            "sell_count",
            "sell_win_count",
            "sell_win_rate",
            "sell_win_rate_posterior",
            "sell_win_rate_lower",
            "avg_sell_win_cny",
            "avg_sell_loss_cny",
            "profit_factor",
            "conservative_sell_expectancy_cny",
            "total_return_pct",
            "max_drawdown_pct",
        )
        sector_policies[sector] = {
            "max_loss_pct": best_params["max_loss_pct"],
            "stop_atr_mult": best_params["stop_atr_mult"],
            "take_profit_r1": best_params["take_profit_r1"],
            "take_profit_r2": best_params["take_profit_r2"],
            "trailing_atr_mult": best_params["trailing_atr_mult"],
            "sample_symbols": symbols,
            "sample_names": names,
            **{key: best_metrics.get(key) for key in metric_keys if key in best_metrics},
            "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        }
        sector_results[sector] = {
            "symbols": names,
            "best": result["best"],
            "top_results": result["top_results"],
            "trial_count": result["trial_count"],
            "discrete_optimization": grid_diagnostics,
        }

    report = {
        "status": "ok",
        "config": {
            "start": start,
            "end": end,
            "initial_cash": initial_cash,
            "fee_rate": fee_rate,
            "max_ops_per_symbol_per_day": max_ops,
            "max_symbols_per_sector": max_symbols_per_sector,
            "max_trials_per_sector": max_trials_per_sector,
            "history_period": history_period,
            "sector_source": sector_source,
            "sector_mapping": sector_by_symbol,
            "sector_warnings": sector_warnings,
            "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        },
        "sector_policies": sector_policies,
        "sector_results": sector_results,
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    env_update: Dict[str, Any] = {"updated": False}
    if _env_bool("INVEST_EXIT_PARAM_OPT_UPDATE_ENV", True):
        env_update = update_env_sector_exit_policies(sector_policies, ROOT / ".env")
        if sector_policies:
            first_policy = next(iter(sector_policies.values()))
            env_update["global_fallback"] = update_env_exit_params(first_policy, ROOT / ".env")

    return {
        "status": "ok",
        "start": start,
        "end": end,
        "sector_count": len(sector_policies),
        "sectors": sorted(sector_policies),
        "env_update": env_update,
        "report_path": str(REPORT_PATH),
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
