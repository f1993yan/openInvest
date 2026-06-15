"""Cached fundamental-data snapshot builder.

This layer fetches best-effort financial ratios from akshare, normalizes them
into the keys consumed by core.fundamental_model, and caches the result locally.
Manual request/config values can still override these automatically fetched
metrics in the backend.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
FUNDAMENTAL_CACHE_DIR = _PROJECT_ROOT / "data" / "fundamentals_cache"
DEFAULT_STALE_DAYS = int(os.getenv("INVEST_FUNDAMENTAL_CACHE_STALE_DAYS", "30"))


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = (
            value.strip()
            .replace(",", "")
            .replace("%", "")
            .replace(" ", "")
        )
        if value in {"", "--", "-", "nan", "None", "null"}:
            return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _ratio_from_percent(value: Any) -> Optional[float]:
    v = _safe_float(value)
    if v is None:
        return None
    return v / 100.0


def _cache_path(symbol: str) -> Path:
    return FUNDAMENTAL_CACHE_DIR / f"{symbol.upper()}.json"


def _cache_fresh(snapshot: Dict[str, Any], stale_days: int) -> bool:
    fetched_at = snapshot.get("fetched_at")
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return datetime.now() - ts <= timedelta(days=stale_days)


def _read_cache(symbol: str) -> Optional[Dict[str, Any]]:
    path = _cache_path(symbol)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("fundamental cache read failed %s: %s", symbol, e)
        return None


def _write_cache(symbol: str, snapshot: Dict[str, Any]) -> None:
    try:
        FUNDAMENTAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(symbol).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("fundamental cache write failed %s: %s", symbol, e)


def _latest_row(df: Any) -> Optional[Any]:
    if df is None or getattr(df, "empty", True):
        return None
    if "REPORT_DATE" in df.columns:
        try:
            tmp = df.copy()
            tmp["_date"] = tmp["REPORT_DATE"].astype(str)
            return tmp.sort_values("_date", ascending=False).iloc[0]
        except Exception:
            return df.iloc[0]
    if "报告期" in df.columns:
        try:
            tmp = df.copy()
            tmp["_date"] = tmp["报告期"].astype(str)
            return tmp.sort_values("_date", ascending=False).iloc[0]
        except Exception:
            return df.iloc[-1]
    if "日期" in df.columns:
        try:
            tmp = df.copy()
            tmp["_date"] = tmp["日期"].astype(str)
            return tmp.sort_values("_date", ascending=False).iloc[0]
        except Exception:
            return df.iloc[0]
    if "date" in df.columns:
        try:
            tmp = df.copy()
            tmp["_date"] = tmp["date"].astype(str)
            return tmp.sort_values("_date", ascending=False).iloc[0]
        except Exception:
            return df.iloc[-1]
    return df.iloc[-1]


def _find_column(
    columns: Iterable[Any],
    keywords: Tuple[str, ...],
    *,
    exclude: Tuple[str, ...] = (),
) -> Optional[str]:
    cols = [str(c) for c in columns]
    for col in cols:
        low = col.lower()
        if all(k.lower() in low for k in keywords) and not any(x.lower() in low for x in exclude):
            return col
    return None


def _matching_columns(
    columns: Iterable[Any],
    keywords: Tuple[str, ...],
    *,
    exclude: Tuple[str, ...] = (),
) -> list[str]:
    out: list[str] = []
    for col in [str(c) for c in columns]:
        low = col.lower()
        if all(k.lower() in low for k in keywords) and not any(x.lower() in low for x in exclude):
            out.append(col)
    return out


def _put_metric(
    metrics: Dict[str, float],
    sources: Dict[str, str],
    key: str,
    value: Any,
    source: str,
    *,
    is_percent: bool = False,
) -> None:
    v = _ratio_from_percent(value) if is_percent else _safe_float(value)
    if v is None:
        return
    metrics[key] = round(float(v), 6)
    sources[key] = source


def _extract_from_financial_indicator(df: Any) -> Tuple[Dict[str, float], Dict[str, str]]:
    metrics: Dict[str, float] = {}
    sources: Dict[str, str] = {}
    row = _latest_row(df)
    if row is None:
        return metrics, sources

    columns = list(getattr(df, "columns", []))
    direct_specs = [
        ("roe", "ROE_AVG", True),
        ("roe", "ROE_YEARLY", True),
        ("roic", "ROIC_YEARLY", True),
        ("gross_margin", "GROSS_PROFIT_RATIO", True),
        ("net_margin", "NET_PROFIT_RATIO", True),
        ("revenue_growth", "OPERATE_INCOME_YOY", True),
        ("profit_growth", "HOLDER_PROFIT_YOY", True),
        ("debt_to_assets", "DEBT_ASSET_RATIO", True),
        ("current_ratio", "CURRENT_RATIO", False),
    ]
    for key, col, is_percent in direct_specs:
        if key in metrics or col not in columns:
            continue
        _put_metric(metrics, sources, key, row.get(col), f"financial_indicator:{col}", is_percent=is_percent)

    specs = [
        ("roe", ("净资产收益率",), (), True),
        ("gross_margin", ("毛利率",), (), True),
        ("gross_margin", ("销售毛利率",), (), True),
        ("operating_margin", ("营业利润率",), (), True),
        ("operating_margin", ("主营业务利润率",), (), True),
        ("revenue_growth", ("收入", "增长率"), (), True),
        ("revenue_growth", ("营收", "增长率"), (), True),
        ("profit_growth", ("净利润", "增长率"), (), True),
        ("debt_to_assets", ("资产负债率",), (), True),
        ("current_ratio", ("流动比率",), (), False),
        ("roic", ("投入资本", "回报率"), (), True),
        ("roic", ("投入资本", "收益率"), (), True),
    ]
    for key, keywords, exclude, is_percent in specs:
        if key in metrics:
            continue
        for col in _matching_columns(columns, keywords, exclude=exclude):
            before = key in metrics
            _put_metric(metrics, sources, key, row.get(col), f"financial_indicator:{col}", is_percent=is_percent)
            if key in metrics and not before:
                break

    return metrics, sources


def _extract_latest_lg_indicator(df: Any) -> Tuple[Dict[str, float], Dict[str, str]]:
    metrics: Dict[str, float] = {}
    sources: Dict[str, str] = {}
    row = _latest_row(df)
    if row is None:
        return metrics, sources

    columns = list(getattr(df, "columns", []))
    col_specs = [
        ("pe_ttm", ("pe_ttm",), False),
        ("pe_ttm", ("市盈率", "ttm"), False),
        ("pe_ttm", ("pe",), False),
        ("pb", ("pb",), False),
        ("pb", ("市净率",), False),
        ("ps", ("ps",), False),
        ("dividend_yield", ("股息率",), True),
        ("dividend_yield", ("dividend",), True),
        ("dividend_yield", ("dv",), True),
    ]
    for key, keywords, is_percent in col_specs:
        if key in metrics:
            continue
        col = _find_column(columns, keywords)
        if col:
            _put_metric(metrics, sources, key, row.get(col), f"lg_indicator:{col}", is_percent=is_percent)
    return metrics, sources


def _fetch_a_share_fundamentals(symbol: str) -> Tuple[Dict[str, float], Dict[str, str], list[str]]:
    import akshare as ak

    metrics: Dict[str, float] = {}
    sources: Dict[str, str] = {}
    warnings: list[str] = []

    try:
        df = ak.stock_financial_analysis_indicator(symbol=symbol)
        m, s = _extract_from_financial_indicator(df)
        metrics.update(m)
        sources.update(s)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"stock_financial_analysis_indicator_failed:{type(e).__name__}")

    if len(metrics) < 4:
        for fn_name in ("stock_financial_abstract_ths", "stock_financial_abstract_new_ths"):
            fn = getattr(ak, fn_name, None)
            if fn is None:
                continue
            try:
                df = fn(symbol=symbol, indicator="按年度")
                m, s = _extract_from_financial_indicator(df)
                metrics.update({k: v for k, v in m.items() if k not in metrics})
                sources.update({k: f"{fn_name}:{v}" for k, v in s.items() if k not in sources})
                if len(metrics) >= 4:
                    break
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{fn_name}_failed:{type(e).__name__}")

    # Official AKShare docs list stock_a_lg_indicator for A-share PE/PB/dividend
    # yield. Some versions exposed stock_a_indicator_lg historically, so try both.
    for fn_name in ("stock_a_lg_indicator", "stock_a_indicator_lg"):
        fn = getattr(ak, fn_name, None)
        if fn is None:
            continue
        try:
            df = fn(symbol=symbol)
            m, s = _extract_latest_lg_indicator(df)
            metrics.update({k: v for k, v in m.items() if k not in metrics})
            sources.update({k: v for k, v in s.items() if k not in sources})
            break
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{fn_name}_failed:{type(e).__name__}")

    if "pe_ttm" not in metrics or "pb" not in metrics:
        try:
            spot = ak.stock_zh_a_spot_em()
            row = spot.loc[spot["代码"].astype(str).str.zfill(6) == symbol].iloc[0]
            if "pe_ttm" not in metrics:
                for col in ("市盈率-动态", "市盈率TTM", "市盈率"):
                    if col in spot.columns:
                        _put_metric(metrics, sources, "pe_ttm", row.get(col), f"spot:{col}")
                        break
            if "pb" not in metrics and "市净率" in spot.columns:
                _put_metric(metrics, sources, "pb", row.get("市净率"), "spot:市净率")
        except Exception:
            # 东方财富现货接口偶发限流；PE/PB 已由乐咕等接口兜底，失败不作为可操作 warning。
            pass

    return metrics, sources, warnings


def _fetch_hk_fundamentals(symbol: str) -> Tuple[Dict[str, float], Dict[str, str], list[str]]:
    import akshare as ak

    metrics: Dict[str, float] = {}
    sources: Dict[str, str] = {}
    warnings: list[str] = []

    try:
        df = ak.stock_financial_hk_analysis_indicator_em(symbol=symbol, indicator="报告期")
        m, s = _extract_from_financial_indicator(df)
        metrics.update(m)
        sources.update(s)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"stock_financial_hk_analysis_indicator_em_failed:{type(e).__name__}")

    # HK support in akshare is less uniform. Cache what is reliably available
    # from valuation indicator interfaces and leave missing statement ratios for
    # manual override unless a connector provides them.
    for indicator, key in (("市盈率", "pe_ttm"), ("市净率", "pb"), ("股息率", "dividend_yield")):
        fn = getattr(ak, "stock_hk_indicator_eniu", None)
        if fn is None:
            break
        try:
            eniu_symbol = symbol if symbol.lower().startswith("hk") else f"hk{symbol}"
            df = fn(symbol=eniu_symbol, indicator=indicator)
            m, s = _extract_latest_lg_indicator(df)
            if key in m:
                metrics.update({k: v for k, v in m.items() if k not in metrics})
                sources.update({k: v for k, v in s.items() if k not in sources})
        except Exception as e:  # noqa: BLE001
            warnings.append(f"stock_hk_indicator_eniu_{indicator}_failed:{type(e).__name__}")

    if "pe_ttm" not in metrics or "pb" not in metrics:
        try:
            spot = ak.stock_hk_spot_em()
            code_col = _find_column(spot.columns, ("代码",)) or _find_column(spot.columns, ("code",))
            if code_col:
                row = spot.loc[spot[code_col].astype(str).str.replace("HK", "", case=False).str.zfill(5) == symbol.zfill(5)].iloc[0]
                if "pe_ttm" not in metrics:
                    for col in ("市盈率", "市盈率TTM", "市盈率-动态"):
                        if col in spot.columns:
                            _put_metric(metrics, sources, "pe_ttm", row.get(col), f"hk_spot:{col}")
                            break
                if "pb" not in metrics:
                    for col in ("市净率", "pb"):
                        if col in spot.columns:
                            _put_metric(metrics, sources, "pb", row.get(col), f"hk_spot:{col}")
                            break
        except Exception as e:  # noqa: BLE001
            warnings.append(f"stock_hk_spot_em_failed:{type(e).__name__}")

    return metrics, sources, warnings


def fetch_fundamental_snapshot(symbol: str, market: str = "a") -> Dict[str, Any]:
    symbol = symbol.strip().upper()
    market_norm = (market or "a").lower()
    if market_norm == "hk" or (symbol.isdigit() and len(symbol) == 5):
        metrics, sources, warnings = _fetch_hk_fundamentals(symbol)
    else:
        metrics, sources, warnings = _fetch_a_share_fundamentals(symbol)

    return {
        "symbol": symbol,
        "market": market_norm,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": metrics,
        "metric_sources": sources,
        "warnings": warnings,
        "source": "akshare",
    }


def get_fundamental_snapshot(
    symbol: str,
    market: str = "a",
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Return cached-or-fetched fundamental metrics.

    If live fetch fails, stale cache is returned with cache_stale=True. If both
    live fetch and cache are unavailable, returns an empty metrics snapshot.
    """
    symbol = symbol.strip().upper()
    cached = _read_cache(symbol)
    if cached and not force_refresh and _cache_fresh(cached, stale_days):
        cached = dict(cached)
        cached["cache_hit"] = True
        cached["cache_stale"] = False
        return cached

    try:
        snapshot = fetch_fundamental_snapshot(symbol, market)
        if snapshot.get("metrics"):
            _write_cache(symbol, snapshot)
        snapshot["cache_hit"] = False
        snapshot["cache_stale"] = False
        return snapshot
    except Exception as e:  # noqa: BLE001
        log.warning("fundamental fetch failed %s: %s", symbol, e)
        if cached:
            cached = dict(cached)
            cached["cache_hit"] = True
            cached["cache_stale"] = True
            cached.setdefault("warnings", []).append(f"live_fetch_failed:{type(e).__name__}")
            return cached
        return {
            "symbol": symbol,
            "market": (market or "a").lower(),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "metrics": {},
            "metric_sources": {},
            "warnings": [f"live_fetch_failed:{type(e).__name__}"],
            "source": "akshare",
            "cache_hit": False,
            "cache_stale": False,
        }


def get_fundamental_metrics(
    symbol: str,
    market: str = "a",
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    force_refresh: bool = False,
) -> Dict[str, float]:
    return get_fundamental_snapshot(
        symbol, market, stale_days=stale_days, force_refresh=force_refresh,
    ).get("metrics", {})


__all__ = [
    "FUNDAMENTAL_CACHE_DIR",
    "fetch_fundamental_snapshot",
    "get_fundamental_metrics",
    "get_fundamental_snapshot",
]
