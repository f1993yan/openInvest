"""Compare arXiv-derived trading models on a frozen Hong Kong equity universe.

The script intentionally stays outside the production decision path. It downloads
adjusted Tencent OHLCV bars, freezes them in a report snapshot, and evaluates all
models with signals delayed by one trading day. Exact paper rules and auditable
paper-inspired proxies are labelled separately in the output.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.compare_upgrade_profitability import (  # noqa: E402
    paired_block_bootstrap_return_difference,
)
from utils.safe_persistence import atomic_write_json, atomic_write_text  # noqa: E402


DEFAULT_SNAPSHOT_PATH = ROOT / "reports" / "hk_arxiv_market_snapshot.json"
DEFAULT_OUTPUT_PATH = ROOT / "reports" / "hk_arxiv_model_comparison.json"
DEFAULT_MARKDOWN_PATH = ROOT / "reports" / "hk_arxiv_model_comparison.md"

# All 93 constituents shown on the AASTOCKS Hang Seng constituent page on
# 2026-08-18. The first 30 are server-rendered and 63 are in its load-more data.
# This avoids using the user's private holdings or watchlist.
DEFAULT_HK_UNIVERSE: Tuple[str, ...] = (
    "00001", "00002", "00003", "00005", "00006", "00012", "00016", "00027",
    "00066", "00101", "00175", "00241", "00267", "00285", "00288", "00291",
    "00300", "00316", "00322", "00386", "00388", "00669", "00688", "00700",
    "00728", "00762", "00823", "00836", "00857", "00868",
    "00883", "00939", "00941", "00960", "00968", "00981", "00992", "01024",
    "01038", "01044", "01088", "01093", "01099", "01109", "01113", "01177",
    "01209", "01211", "01299", "01378", "01398", "01519", "01801", "01810",
    "01876", "01928", "01929", "01997", "02015", "02020", "02057", "02269",
    "02313", "02318", "02319", "02331", "02359", "02382", "02388", "02600",
    "02618", "02628", "02688", "02899", "03690", "03692", "03750", "03968",
    "03988", "03993", "06160", "06181", "06618", "06690", "06862", "09618",
    "09633", "09888", "09901", "09961", "09988", "09992", "09999",
)

PAPER_REFERENCES: Dict[str, Dict[str, str]] = {
    "follow_bb": {
        "arxiv_id": "1401.1892",
        "title": "Dynamical Models of Stock Prices Based on Technical Trading Rules Part III: Application to Hong Kong Stocks",
        "url": "https://arxiv.org/abs/1401.1892",
        "fidelity": "exact_signal_rule",
        "implementation": "Exact RLS signal rule with 3-day smoothing; the portfolio wrapper uses equal rather than HSI weights.",
    },
    "ride_mood": {
        "arxiv_id": "1401.1892",
        "title": "Dynamical Models of Stock Prices Based on Technical Trading Rules Part III: Application to Hong Kong Stocks",
        "url": "https://arxiv.org/abs/1401.1892",
        "fidelity": "exact_signal_rule",
        "implementation": "Exact RLS signal rule with 5-day mood smoothing; the portfolio wrapper uses equal rather than HSI weights.",
    },
    "trend_follow_5_60": {
        "arxiv_id": "1401.1892",
        "title": "Dynamical Models of Stock Prices Based on Technical Trading Rules Part III: Application to Hong Kong Stocks",
        "url": "https://arxiv.org/abs/1401.1892",
        "fidelity": "exact_signal_rule",
        "implementation": "Exact 5/60-day signal rule; the portfolio wrapper uses equal rather than HSI weights.",
    },
    "ema_trend": {
        "arxiv_id": "2504.10914",
        "title": "Breaking the Trend: How to Avoid Cherry-Picked Signals",
        "url": "https://arxiv.org/abs/2504.10914",
        "fidelity": "paper_inspired_proxy",
        "implementation": "One fixed 100-day EMA return signal with no parameter basket.",
    },
    "spatio_temporal": {
        "arxiv_id": "2302.10175",
        "title": "Spatio-Temporal Momentum",
        "url": "https://arxiv.org/abs/2302.10175",
        "fidelity": "paper_inspired_proxy",
        "implementation": "Transparent linear time-series and cross-sectional momentum proxy; not the neural network.",
    },
    "low_volatility": {
        "arxiv_id": "2003.08302",
        "title": "The Low-volatility Anomaly and the Adaptive Multi-Factor Model",
        "url": "https://arxiv.org/abs/2003.08302",
        "fidelity": "paper_inspired_proxy",
        "implementation": "Long-only low-volatility rank gated by a positive 120-day trend.",
    },
    "drift_reversal": {
        "arxiv_id": "2511.12490",
        "title": "Discovery of a 13-Sharpe OOS Factor: Drift Regimes Unlock Hidden Cross-Sectional Predictability",
        "url": "https://arxiv.org/abs/2511.12490",
        "fidelity": "paper_inspired_proxy",
        "implementation": "Positive-day drift regime plus five-day reversal; the point-in-time value leg is omitted.",
    },
}


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    portfolio_mode: str
    rebalance_days: int = 1
    top_k: int = 4
    gross_exposure: float = 0.95
    max_weight: float = 0.35


MODEL_SPECS: Tuple[ModelSpec, ...] = (
    ModelSpec("follow_bb", "Follow-the-Big-Buyer (FollowBB)", "independent_sleeves"),
    ModelSpec("ride_mood", "Ride-the-Mood (RideMood)", "independent_sleeves"),
    ModelSpec("trend_follow_5_60", "5/60-day trend following", "independent_sleeves"),
    ModelSpec("ema_trend", "Fixed 100-day EMA trend", "independent_sleeves"),
    ModelSpec("spatio_temporal", "Spatio-temporal momentum proxy", "cross_section", rebalance_days=20),
    ModelSpec("low_volatility", "Low-volatility trend proxy", "cross_section", rebalance_days=20),
    ModelSpec("drift_reversal", "Drift-regime reversal proxy", "cross_section", rebalance_days=20),
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _default_completed_hk_date() -> date:
    now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    candidate = now.date()
    if now.time() < clock_time(16, 15):
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _tencent_history_url(code: str, bars: int = 800) -> str:
    return (
        "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?"
        f"param={code},day,,,{min(max(int(bars), 5), 800)},qfq"
    )


def fetch_tencent_hk_history(
    symbol: str,
    *,
    bars: int = 800,
    as_of: date | None = None,
    index_symbol: bool = False,
) -> pd.DataFrame:
    """Fetch adjusted Tencent daily bars while preserving the opening price."""
    code = "hkHSI" if index_symbol else f"hk{str(symbol).zfill(5)}"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(
                _tencent_history_url(code, bars),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=20,
            )
            response.raise_for_status()
            node = (response.json().get("data") or {}).get(code) or {}
            rows = node.get("qfqday") or node.get("day") or []
            records: List[Dict[str, Any]] = []
            for row in rows:
                if len(row) < 6:
                    continue
                records.append({
                    "Date": str(row[0]),
                    "Open": row[1],
                    "Close": row[2],
                    "High": row[3],
                    "Low": row[4],
                    "Volume": row[5],
                })
            frame = pd.DataFrame.from_records(records)
            if frame.empty:
                raise ValueError(f"Tencent returned no daily bars for {code}")
            frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
            for column in ("Open", "High", "Low", "Close", "Volume"):
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame = frame.dropna(subset=["Date", "Open", "High", "Low", "Close"])
            frame = frame[(frame[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
            frame["Volume"] = frame["Volume"].fillna(0.0).clip(lower=0.0)
            frame = frame.drop_duplicates("Date", keep="last").sort_values("Date").set_index("Date")
            if as_of is not None:
                frame = frame[frame.index.date <= as_of]
            if frame.empty:
                raise ValueError(f"No completed bars remain for {code} at {as_of}")
            return frame.astype(float)
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {code}: {last_error}")


def _frame_to_records(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for timestamp, row in frame.iterrows():
        records.append({
            "date": timestamp.strftime("%Y-%m-%d"),
            "open": round(_safe_float(row["Open"]), 6),
            "high": round(_safe_float(row["High"]), 6),
            "low": round(_safe_float(row["Low"]), 6),
            "close": round(_safe_float(row["Close"]), 6),
            "volume": round(_safe_float(row["Volume"]), 3),
        })
    return records


def _records_to_frame(records: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame.from_records(records)
    if frame.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume",
    })
    for column in ("Open", "High", "Low", "Close", "Volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["date", "Open", "Close"]).set_index("date").sort_index()


def create_market_snapshot(
    symbols: Sequence[str],
    *,
    as_of: date,
    path: Path,
    workers: int = 8,
) -> Tuple[Dict[str, pd.DataFrame], pd.DataFrame, Dict[str, Any]]:
    histories: Dict[str, pd.DataFrame] = {}
    errors: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 12))) as pool:
        futures = {
            pool.submit(fetch_tencent_hk_history, symbol, as_of=as_of): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                histories[symbol] = future.result()
            except Exception as exc:
                errors[symbol] = str(exc)
    benchmark = fetch_tencent_hk_history("HSI", as_of=as_of, index_symbol=True)
    ordered = {symbol: histories[symbol] for symbol in symbols if symbol in histories}
    manifest = {
        symbol: {
            "bars": len(frame),
            "first_date": frame.index[0].strftime("%Y-%m-%d"),
            "last_date": frame.index[-1].strftime("%Y-%m-%d"),
            "missing_ohlc_rows": int(frame[["Open", "High", "Low", "Close"]].isna().any(axis=1).sum()),
        }
        for symbol, frame in ordered.items()
    }
    payload = {
        "schema_version": 1,
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of_date": as_of.isoformat(),
        "source": {
            "price_provider": "Tencent qfq daily K-line",
            "price_url_template": "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?param=hk{symbol},day,,,800,qfq",
            "universe_provider": "AASTOCKS Hang Seng constituent page, all 93 rows including load-more data",
            "universe_url": "https://www.aastocks.com/en/stocks/market/index/hk-index-con.aspx",
        },
        "requested_symbols": list(symbols),
        "successful_symbols": list(ordered),
        "fetch_errors": errors,
        "manifest": manifest,
        "histories": {symbol: _frame_to_records(frame) for symbol, frame in ordered.items()},
        "benchmark_hsi": _frame_to_records(benchmark),
    }
    atomic_write_json(path, payload)
    return ordered, benchmark, payload


def load_market_snapshot(
    path: Path,
    *,
    expected_as_of: date | None = None,
) -> Tuple[Dict[str, pd.DataFrame], pd.DataFrame, Dict[str, Any]]:
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError(f"Unsupported HK snapshot schema: {payload.get('schema_version')}")
    if expected_as_of is not None and payload.get("as_of_date") != expected_as_of.isoformat():
        raise ValueError(
            f"Snapshot as-of {payload.get('as_of_date')} does not match requested {expected_as_of}; use --refresh-snapshot"
        )
    histories = {
        str(symbol): _records_to_frame(records)
        for symbol, records in dict(payload.get("histories") or {}).items()
    }
    benchmark = _records_to_frame(payload.get("benchmark_hsi") or [])
    return histories, benchmark, payload


def fuzzy_excess_demands(x: float, *, w: float = 0.01) -> np.ndarray:
    """Paper equations (5) and (6): big-seller and big-buyer excess demand."""
    if w <= 0:
        raise ValueError("w must be positive")
    value = _safe_float(x)
    if value <= 0.0:
        ed6 = 0.0
    elif value <= 2.0 * w:
        ed6 = -0.1 * value / w
    elif value <= 3.0 * w:
        ed6 = -0.2 * value / w + 0.2
    else:
        ed6 = -0.4

    if value <= -3.0 * w:
        ed7 = 0.4
    elif value <= -2.0 * w:
        ed7 = -0.2 * value / w - 0.2
    elif value <= 0.0:
        ed7 = -0.1 * value / w
    else:
        ed7 = 0.0
    return np.array([ed6, ed7], dtype=float)


def estimate_big_trader_strengths(
    close: pd.Series,
    *,
    n: int = 3,
    w: float = 0.01,
    forgetting: float = 0.95,
    gamma: float = 10.0,
) -> pd.DataFrame:
    """Exponentially-forgetting RLS from arXiv:1401.1892 equations (11)-(13)."""
    prices = pd.to_numeric(close, errors="coerce").astype(float)
    result = pd.DataFrame(index=prices.index, columns=["a6", "a7"], dtype=float)
    if len(prices) <= n:
        return result
    if not 0.0 < forgetting < 1.0:
        raise ValueError("forgetting must be between zero and one")
    values = prices.to_numpy(dtype=float)
    features: List[np.ndarray | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1:i + 1]
        if not np.isfinite(window).all() or np.any(window <= 0):
            continue
        x = math.log(values[i] / float(np.mean(window)))
        features[i] = fuzzy_excess_demands(x, w=w)

    estimate = np.zeros(2, dtype=float)
    covariance = np.eye(2, dtype=float) * gamma
    identity = np.eye(2, dtype=float)
    for i in range(n, len(values)):
        phi = features[i - 1]
        if phi is None or not np.isfinite(values[i - 1:i + 1]).all() or np.any(values[i - 1:i + 1] <= 0):
            continue
        observed_return = math.log(values[i] / values[i - 1])
        denominator = forgetting + float(phi @ covariance @ phi)
        if denominator <= 1e-12 or not math.isfinite(denominator):
            continue
        gain = covariance @ phi / denominator
        estimate = estimate + gain * (observed_return - float(phi @ estimate))
        covariance = (identity - np.outer(gain, phi)) @ covariance / forgetting
        covariance = (covariance + covariance.T) / 2.0
        result.iloc[i] = estimate
    return result


def _stateful_positive_score(
    entry: pd.Series,
    exit_signal: pd.Series,
    strength: pd.Series,
) -> pd.Series:
    active = False
    output = pd.Series(0.0, index=entry.index, dtype=float)
    for timestamp in entry.index:
        enter = bool(entry.at[timestamp]) if pd.notna(entry.at[timestamp]) else False
        leave = bool(exit_signal.at[timestamp]) if pd.notna(exit_signal.at[timestamp]) else False
        if not active and enter:
            active = True
        elif active and leave:
            active = False
        if active:
            output.at[timestamp] = max(_safe_float(strength.at[timestamp]), 1e-12)
    return output


def build_model_scores(
    histories: Mapping[str, pd.DataFrame],
) -> Tuple[Dict[str, pd.DataFrame], pd.DataFrame, pd.DatetimeIndex]:
    calendar = pd.DatetimeIndex(sorted(set().union(*(set(frame.index) for frame in histories.values()))))
    closes = pd.DataFrame({symbol: frame["Close"] for symbol, frame in histories.items()}).reindex(calendar)
    returns = closes.pct_change(fill_method=None)
    vol20 = returns.rolling(20, min_periods=12).std(ddof=1) * math.sqrt(252.0)
    vol60 = returns.rolling(60, min_periods=40).std(ddof=1) * math.sqrt(252.0)
    ret5 = closes / closes.shift(5) - 1.0
    ret20 = closes / closes.shift(20) - 1.0
    ret60 = closes / closes.shift(60) - 1.0
    ret120 = closes / closes.shift(120) - 1.0
    ema100 = closes.ewm(span=100, adjust=False, min_periods=100).mean()
    ema120 = closes.ewm(span=120, adjust=False, min_periods=120).mean()
    ema_return100 = returns.ewm(span=100, adjust=False, min_periods=100).mean()
    positive_ratio63 = (returns > 0).rolling(63, min_periods=45).mean()

    follow_columns: Dict[str, pd.Series] = {}
    mood_columns: Dict[str, pd.Series] = {}
    trend_columns: Dict[str, pd.Series] = {}
    ema_columns: Dict[str, pd.Series] = {}
    for symbol, frame in histories.items():
        close = pd.to_numeric(frame["Close"], errors="coerce")
        strengths = estimate_big_trader_strengths(close)
        follow_a6 = strengths["a6"].rolling(3, min_periods=3).mean()
        follow_a7 = strengths["a7"].rolling(3, min_periods=3).mean()
        follow_columns[symbol] = _stateful_positive_score(
            (follow_a7 > 0.0) & (follow_a6 <= 0.0),
            follow_a7 < 0.0,
            follow_a7 + (-follow_a6).clip(lower=0.0),
        )
        mood_a6 = strengths["a6"].rolling(5, min_periods=5).mean()
        mood_a7 = strengths["a7"].rolling(5, min_periods=5).mean()
        mood = mood_a7 - mood_a6
        mood_columns[symbol] = _stateful_positive_score(mood > 0.0, mood < 0.0, mood)
        short = close.rolling(5, min_periods=5).mean()
        long = close.rolling(60, min_periods=60).mean()
        spread = short / long - 1.0
        trend_columns[symbol] = spread.where(spread > 0.0, 0.0)
        local_returns = close.pct_change(fill_method=None)
        local_vol = local_returns.rolling(20, min_periods=12).std(ddof=1) * math.sqrt(252.0)
        local_ema_price = close.ewm(span=100, adjust=False, min_periods=100).mean()
        local_ema_return = local_returns.ewm(span=100, adjust=False, min_periods=100).mean()
        ema_score = local_ema_return / local_vol.clip(lower=0.08)
        ema_columns[symbol] = ema_score.where((local_ema_return > 0.0) & (close > local_ema_price), 0.0)

    def panel(columns: Mapping[str, pd.Series]) -> pd.DataFrame:
        # Zero means inactive. Missing exchange dates retain the last observed state.
        return pd.DataFrame(columns).reindex(calendar).ffill().fillna(0.0)

    def carry_suspensions(values: pd.DataFrame) -> pd.DataFrame:
        # A missing close is a suspension/data gap, not a new bearish signal.
        return values.where(closes.notna()).ffill().fillna(0.0)

    rank = lambda values: values.rank(axis=1, method="average", pct=True, ascending=True)
    temporal = (
        ret20 / vol20.clip(lower=0.08)
        + ret60 / vol60.clip(lower=0.08)
        + ret120 / vol60.clip(lower=0.08)
    ) / 3.0
    spatio = 0.55 * rank(temporal) + 0.45 * rank(ret60)
    spatio = spatio.where((ret20 > 0.0) & (ret60 > 0.0), 0.0).fillna(0.0)
    low_vol = 0.70 * rank(-vol60) + 0.30 * rank(ret120)
    low_vol = low_vol.where((ret120 > 0.0) & (closes > ema120), 0.0).fillna(0.0)
    drift = 0.65 * rank(-ret5) + 0.35 * rank(ret60 / vol60.clip(lower=0.08))
    drift = drift.where((positive_ratio63 >= 0.60) & (ret60 > 0.0), 0.0).fillna(0.0)

    scores = {
        "follow_bb": panel(follow_columns),
        "ride_mood": panel(mood_columns),
        "trend_follow_5_60": panel(trend_columns),
        "ema_trend": panel(ema_columns),
        "spatio_temporal": carry_suspensions(spatio.reindex(calendar)),
        "low_volatility": carry_suspensions(low_vol.reindex(calendar)),
        "drift_reversal": carry_suspensions(drift.reindex(calendar)),
    }
    return scores, vol20.reindex(calendar), calendar


def _market_panels(
    histories: Mapping[str, pd.DataFrame],
    calendar: pd.DatetimeIndex,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    opens = pd.DataFrame({symbol: frame["Open"] for symbol, frame in histories.items()}).reindex(calendar)
    closes = pd.DataFrame({symbol: frame["Close"] for symbol, frame in histories.items()}).reindex(calendar)
    return opens, closes


def _evaluation_dates(calendar: pd.DatetimeIndex, start: str, end: str) -> pd.DatetimeIndex:
    dates = calendar[(calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))]
    if len(dates) < 2:
        raise ValueError(f"Evaluation period {start}..{end} has fewer than two trading days")
    return dates


def _previous_calendar_date(calendar: pd.DatetimeIndex, day: pd.Timestamp) -> pd.Timestamp | None:
    position = int(calendar.searchsorted(day, side="left")) - 1
    return calendar[position] if position >= 0 else None


def _metrics(
    curve: Sequence[Dict[str, Any]],
    trades: Sequence[Dict[str, Any]],
    *,
    initial_cash: float,
) -> Dict[str, Any]:
    values = [_safe_float(row.get("equity"), initial_cash) for row in curve]
    daily = np.asarray([
        current / previous - 1.0
        for previous, current in zip([initial_cash, *values[:-1]], values)
        if previous > 0 and current > 0
    ], dtype=float)
    final_equity = values[-1] if values else initial_cash
    total_return = final_equity / initial_cash - 1.0 if initial_cash > 0 else 0.0
    years = max(len(values) / 252.0, 1.0 / 252.0)
    annualized_return = (1.0 + total_return) ** (1.0 / years) - 1.0 if total_return > -1.0 else -1.0
    annualized_vol = float(np.std(daily, ddof=1) * math.sqrt(252.0)) if len(daily) > 1 else 0.0
    mean_daily = float(np.mean(daily)) if len(daily) else 0.0
    downside = np.minimum(daily, 0.0)
    downside_dev = float(math.sqrt(np.mean(np.square(downside)))) if len(downside) else 0.0
    sortino = mean_daily * 252.0 / (downside_dev * math.sqrt(252.0)) if downside_dev > 1e-12 else 0.0
    sharpe = mean_daily * 252.0 / annualized_vol if annualized_vol > 1e-12 else 0.0
    peak = initial_cash
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = min(max_drawdown, value / peak - 1.0)
    traded_notional = sum(_safe_float(row.get("notional")) for row in trades)
    fees = sum(_safe_float(row.get("fee")) for row in trades)
    average_equity = float(np.mean(values)) if values else initial_cash
    exposures = [
        _safe_float(row.get("market_value")) / _safe_float(row.get("equity"), 1.0)
        for row in curve
        if _safe_float(row.get("equity")) > 0
    ]
    return {
        "initial_cash_hkd": round(initial_cash, 2),
        "final_equity_hkd": round(final_equity, 2),
        "total_return_pct": round(total_return * 100.0, 4),
        "annualized_return_pct": round(annualized_return * 100.0, 4),
        "annualized_volatility_pct": round(annualized_vol * 100.0, 4),
        "max_drawdown_pct": round(max_drawdown * 100.0, 4),
        "sharpe_ratio": round(sharpe, 6),
        "sortino_ratio": round(sortino, 6),
        "calmar_ratio": round(annualized_return / abs(max_drawdown), 6) if max_drawdown < -1e-12 else 0.0,
        "turnover_pct": round(traded_notional / average_equity * 100.0, 4) if average_equity > 0 else 0.0,
        "average_exposure_pct": round(float(np.mean(exposures)) * 100.0, 4) if exposures else 0.0,
        "total_fees_hkd": round(fees, 2),
        "trade_count": len(trades),
        "buy_count": sum(1 for row in trades if row.get("side") == "BUY"),
        "sell_count": sum(1 for row in trades if row.get("side") == "SELL"),
        "trading_days": len(values),
    }


def run_independent_sleeves(
    *,
    spec: ModelSpec,
    histories: Mapping[str, pd.DataFrame],
    scores: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    start: str,
    end: str,
    initial_cash: float,
    fee_rate: float,
) -> Dict[str, Any]:
    symbols = list(histories)
    dates = _evaluation_dates(calendar, start, end)
    opens, closes = _market_panels(histories, calendar)
    sleeve_cash = {symbol: initial_cash / len(symbols) for symbol in symbols}
    shares = {symbol: 0 for symbol in symbols}
    last_close = {
        symbol: _safe_float(frame.loc[frame.index < dates[0], "Close"].iloc[-1])
        for symbol, frame in histories.items()
        if not frame.loc[frame.index < dates[0]].empty
    }
    trades: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []
    for day in dates:
        decision_day = _previous_calendar_date(calendar, day)
        for symbol in symbols:
            open_price = _safe_float(opens.at[day, symbol], 0.0) if day in opens.index else 0.0
            score = _safe_float(scores.at[decision_day, symbol], 0.0) if decision_day is not None else 0.0
            active = score > 0.0
            if open_price <= 0:
                continue
            if shares[symbol] <= 0 and active:
                quantity = int(sleeve_cash[symbol] // (open_price * (1.0 + fee_rate)))
                if quantity > 0:
                    gross = quantity * open_price
                    fee = gross * fee_rate
                    sleeve_cash[symbol] -= gross + fee
                    shares[symbol] = quantity
                    trades.append({
                        "date": day.strftime("%Y-%m-%d"), "symbol": symbol, "side": "BUY",
                        "shares": quantity, "price": round(open_price, 4), "notional": round(gross, 2),
                        "fee": round(fee, 2), "decision_date": decision_day.strftime("%Y-%m-%d"),
                    })
            elif shares[symbol] > 0 and not active:
                quantity = shares[symbol]
                gross = quantity * open_price
                fee = gross * fee_rate
                sleeve_cash[symbol] += gross - fee
                shares[symbol] = 0
                trades.append({
                    "date": day.strftime("%Y-%m-%d"), "symbol": symbol, "side": "SELL",
                    "shares": quantity, "price": round(open_price, 4), "notional": round(gross, 2),
                    "fee": round(fee, 2), "decision_date": decision_day.strftime("%Y-%m-%d"),
                })
        market_value = 0.0
        for symbol in symbols:
            close_price = _safe_float(closes.at[day, symbol], last_close.get(symbol, 0.0))
            if close_price > 0:
                last_close[symbol] = close_price
            market_value += shares[symbol] * last_close.get(symbol, 0.0)
        cash = sum(sleeve_cash.values())
        curve.append({
            "date": day.strftime("%Y-%m-%d"), "cash": round(cash, 2),
            "market_value": round(market_value, 2), "equity": round(cash + market_value, 2),
        })
    return {
        "metrics": _metrics(curve, trades, initial_cash=initial_cash),
        "trades": trades,
        "equity_curve": curve,
    }


def _capped_inverse_vol_weights(
    selected: Sequence[str],
    risk: Mapping[str, float],
    *,
    gross_exposure: float,
    max_weight: float,
) -> Dict[str, float]:
    if not selected:
        return {}
    inverse = {symbol: 1.0 / max(_safe_float(risk.get(symbol), 0.30), 0.08) for symbol in selected}
    free = set(selected)
    weights: Dict[str, float] = {}
    remaining = gross_exposure
    while free and remaining > 1e-12:
        denominator = sum(inverse[symbol] for symbol in free)
        proposed = {symbol: remaining * inverse[symbol] / denominator for symbol in free}
        capped = {symbol for symbol, value in proposed.items() if value > max_weight + 1e-12}
        if not capped:
            weights.update(proposed)
            break
        for symbol in capped:
            weights[symbol] = max_weight
            free.remove(symbol)
            remaining -= max_weight
    return weights


def run_cross_section(
    *,
    spec: ModelSpec,
    histories: Mapping[str, pd.DataFrame],
    scores: pd.DataFrame,
    vol20: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    start: str,
    end: str,
    initial_cash: float,
    fee_rate: float,
) -> Dict[str, Any]:
    dates = _evaluation_dates(calendar, start, end)
    opens, closes = _market_panels(histories, calendar)
    cash = float(initial_cash)
    positions: Dict[str, int] = {}
    last_close: Dict[str, float] = {}
    trades: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []
    for symbol, frame in histories.items():
        prior = frame[frame.index < dates[0]]
        if not prior.empty:
            last_close[symbol] = _safe_float(prior["Close"].iloc[-1])

    for day_index, day in enumerate(dates):
        calendar_position = int(calendar.searchsorted(day, side="left"))
        # Establish a portfolio on the first evaluation day, then keep one
        # fixed market-calendar anchor across full, rolling, and holdout tests.
        if day_index == 0 or calendar_position % max(1, spec.rebalance_days) == 0:
            decision_day = _previous_calendar_date(calendar, day)
            row = scores.loc[decision_day] if decision_day is not None else pd.Series(dtype=float)
            clean = row.replace([np.inf, -np.inf], np.nan).dropna()
            selected = list(clean[clean > 0.0].sort_values(ascending=False).head(spec.top_k).index)
            risk = {
                symbol: _safe_float(vol20.at[decision_day, symbol], 0.30)
                for symbol in selected
            } if decision_day is not None else {}
            weights = _capped_inverse_vol_weights(
                selected, risk, gross_exposure=spec.gross_exposure, max_weight=spec.max_weight,
            )
            opening_equity = cash
            for symbol, quantity in positions.items():
                open_price = _safe_float(opens.at[day, symbol], last_close.get(symbol, 0.0))
                opening_equity += quantity * open_price
            desired: Dict[str, int] = {}
            for symbol, weight in weights.items():
                open_price = _safe_float(opens.at[day, symbol], 0.0)
                if open_price > 0:
                    desired[symbol] = int(opening_equity * weight // open_price)

            for symbol, current in list(positions.items()):
                quantity = current - max(0, desired.get(symbol, 0))
                open_price = _safe_float(opens.at[day, symbol], 0.0)
                if quantity <= 0 or open_price <= 0:
                    continue
                gross = quantity * open_price
                fee = gross * fee_rate
                cash += gross - fee
                positions[symbol] -= quantity
                trades.append({
                    "date": day.strftime("%Y-%m-%d"), "symbol": symbol, "side": "SELL",
                    "shares": quantity, "price": round(open_price, 4), "notional": round(gross, 2),
                    "fee": round(fee, 2), "decision_date": decision_day.strftime("%Y-%m-%d"),
                })
                if positions[symbol] <= 0:
                    del positions[symbol]

            for symbol in selected:
                open_price = _safe_float(opens.at[day, symbol], 0.0)
                current = positions.get(symbol, 0)
                quantity = desired.get(symbol, 0) - current
                if quantity <= 0 or open_price <= 0:
                    continue
                affordable = int(cash // (open_price * (1.0 + fee_rate)))
                quantity = min(quantity, affordable)
                if quantity <= 0:
                    continue
                gross = quantity * open_price
                fee = gross * fee_rate
                cash -= gross + fee
                positions[symbol] = current + quantity
                trades.append({
                    "date": day.strftime("%Y-%m-%d"), "symbol": symbol, "side": "BUY",
                    "shares": quantity, "price": round(open_price, 4), "notional": round(gross, 2),
                    "fee": round(fee, 2), "decision_date": decision_day.strftime("%Y-%m-%d"),
                })

        market_value = 0.0
        for symbol, quantity in positions.items():
            close_price = _safe_float(closes.at[day, symbol], last_close.get(symbol, 0.0))
            if close_price > 0:
                last_close[symbol] = close_price
            market_value += quantity * last_close.get(symbol, 0.0)
        for symbol in histories:
            close_price = _safe_float(closes.at[day, symbol], 0.0)
            if close_price > 0:
                last_close[symbol] = close_price
        curve.append({
            "date": day.strftime("%Y-%m-%d"), "cash": round(cash, 2),
            "market_value": round(market_value, 2), "equity": round(cash + market_value, 2),
        })
    return {
        "metrics": _metrics(curve, trades, initial_cash=initial_cash),
        "trades": trades,
        "equity_curve": curve,
    }


def run_model(
    spec: ModelSpec,
    *,
    histories: Mapping[str, pd.DataFrame],
    scores: Mapping[str, pd.DataFrame],
    vol20: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    start: str,
    end: str,
    initial_cash: float,
    fee_rate: float,
) -> Dict[str, Any]:
    common = dict(
        spec=spec, histories=histories, scores=scores[spec.key], calendar=calendar,
        start=start, end=end, initial_cash=initial_cash, fee_rate=fee_rate,
    )
    if spec.portfolio_mode == "independent_sleeves":
        return run_independent_sleeves(**common)
    return run_cross_section(vol20=vol20, **common)


def _buy_hold_scores(histories: Mapping[str, pd.DataFrame], calendar: pd.DatetimeIndex) -> pd.DataFrame:
    values = pd.DataFrame({symbol: frame["Close"].notna().astype(float) for symbol, frame in histories.items()})
    return values.reindex(calendar).ffill().fillna(0.0)


def _naive_momentum_scores(
    histories: Mapping[str, pd.DataFrame],
    calendar: pd.DatetimeIndex,
    periods: int,
) -> pd.DataFrame:
    closes = pd.DataFrame({symbol: frame["Close"] for symbol, frame in histories.items()}).reindex(calendar)
    momentum = closes / closes.shift(periods) - 1.0
    scores = momentum.rank(axis=1, method="average", pct=True)
    return scores.where(momentum > 0.0, 0.0).where(closes.notna()).ffill().fillna(0.0)


def _window_boundaries(
    dates: pd.DatetimeIndex,
    *,
    holdout_days: int,
    window_days: int,
    max_windows: int = 4,
) -> Tuple[pd.DatetimeIndex, List[Tuple[pd.Timestamp, pd.Timestamp]]]:
    holdout_size = min(max(40, int(holdout_days)), max(40, len(dates) // 3))
    holdout = dates[-holdout_size:]
    development = dates[:-holdout_size]
    windows: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    stop = len(development)
    while stop >= window_days and len(windows) < max_windows:
        block = development[stop - window_days:stop]
        windows.append((block[0], block[-1]))
        stop -= window_days
    windows.reverse()
    return holdout, windows


def _compact_metrics(metrics: Mapping[str, Any]) -> Dict[str, Any]:
    keys = (
        "total_return_pct", "annualized_return_pct", "max_drawdown_pct", "sortino_ratio",
        "sharpe_ratio", "turnover_pct", "average_exposure_pct", "trade_count", "total_fees_hkd",
    )
    return {key: metrics.get(key) for key in keys}


def compare_models(
    *,
    histories: Mapping[str, pd.DataFrame],
    benchmark: pd.DataFrame,
    snapshot: Mapping[str, Any],
    start: str,
    end: str,
    initial_cash: float,
    fee_rate: float,
    stress_fee_rate: float,
    holdout_days: int = 126,
    window_days: int = 126,
) -> Dict[str, Any]:
    scores, vol20, calendar = build_model_scores(histories)
    dates = _evaluation_dates(calendar, start, end)
    holdout, development_windows = _window_boundaries(
        dates, holdout_days=holdout_days, window_days=window_days,
    )
    buy_hold_spec = ModelSpec("equal_weight_buy_hold", "Equal-weight buy and hold", "independent_sleeves")
    naive_specs = (
        ModelSpec("naive_momentum_60", "Naive 60-day momentum", "cross_section", rebalance_days=20),
        ModelSpec("naive_momentum_120", "Naive 120-day momentum", "cross_section", rebalance_days=20),
    )
    all_scores = dict(scores)
    all_scores[buy_hold_spec.key] = _buy_hold_scores(histories, calendar)
    all_scores["naive_momentum_60"] = _naive_momentum_scores(histories, calendar, 60)
    all_scores["naive_momentum_120"] = _naive_momentum_scores(histories, calendar, 120)

    def execute(spec: ModelSpec, left: str, right: str, cost: float) -> Dict[str, Any]:
        return run_model(
            spec, histories=histories, scores=all_scores, vol20=vol20, calendar=calendar,
            start=left, end=right, initial_cash=initial_cash, fee_rate=cost,
        )

    baseline_full = execute(buy_hold_spec, start, end, fee_rate)
    baseline_holdout = execute(
        buy_hold_spec, holdout[0].strftime("%Y-%m-%d"), holdout[-1].strftime("%Y-%m-%d"), fee_rate,
    )
    baseline_windows = [
        execute(buy_hold_spec, left.strftime("%Y-%m-%d"), right.strftime("%Y-%m-%d"), fee_rate)
        for left, right in development_windows
    ]

    benchmark_histories = {"HSI": benchmark}
    benchmark_calendar = benchmark.index
    benchmark_scores = {"equal_weight_buy_hold": _buy_hold_scores(benchmark_histories, benchmark_calendar)}
    benchmark_full = run_independent_sleeves(
        spec=buy_hold_spec, histories=benchmark_histories, scores=benchmark_scores["equal_weight_buy_hold"],
        calendar=benchmark_calendar, start=start, end=end, initial_cash=initial_cash, fee_rate=fee_rate,
    )
    naive_benchmarks: Dict[str, Any] = {}
    for spec in naive_specs:
        full = execute(spec, start, end, fee_rate)
        holdout_result = execute(
            spec, holdout[0].strftime("%Y-%m-%d"), holdout[-1].strftime("%Y-%m-%d"), fee_rate,
        )
        naive_benchmarks[spec.key] = {
            "label": spec.label,
            "full_sample": _compact_metrics(full["metrics"]),
            "holdout": _compact_metrics(holdout_result["metrics"]),
        }

    details: List[Dict[str, Any]] = []
    for spec in MODEL_SPECS:
        full = execute(spec, start, end, fee_rate)
        holdout_result = execute(
            spec, holdout[0].strftime("%Y-%m-%d"), holdout[-1].strftime("%Y-%m-%d"), fee_rate,
        )
        window_rows: List[Dict[str, Any]] = []
        for (left, right), base in zip(development_windows, baseline_windows):
            candidate = execute(spec, left.strftime("%Y-%m-%d"), right.strftime("%Y-%m-%d"), fee_rate)
            candidate_return = _safe_float(candidate["metrics"].get("total_return_pct"))
            baseline_return = _safe_float(base["metrics"].get("total_return_pct"))
            window_rows.append({
                "start": left.strftime("%Y-%m-%d"),
                "end": right.strftime("%Y-%m-%d"),
                "return_pct": round(candidate_return, 4),
                "buy_hold_return_pct": round(baseline_return, 4),
                "excess_vs_buy_hold_pp": round(candidate_return - baseline_return, 4),
                "max_drawdown_pct": candidate["metrics"]["max_drawdown_pct"],
                "sortino_ratio": candidate["metrics"]["sortino_ratio"],
            })
        stressed = execute(spec, start, end, stress_fee_rate)
        anchor_returns: List[float] = []
        for offset in (0, 5, 10, 15):
            if offset >= len(dates) - 40:
                continue
            anchored = execute(spec, dates[offset].strftime("%Y-%m-%d"), end, fee_rate)
            anchor_returns.append(_safe_float(anchored["metrics"].get("annualized_return_pct")))

        development_excess = [row["excess_vs_buy_hold_pp"] for row in window_rows]
        development_returns = [row["return_pct"] for row in window_rows]
        holdout_return = _safe_float(holdout_result["metrics"].get("total_return_pct"))
        holdout_base_return = _safe_float(baseline_holdout["metrics"].get("total_return_pct"))
        bootstrap = paired_block_bootstrap_return_difference(
            baseline_full["equity_curve"], full["equity_curve"], block_size=20, samples=2000,
            seed=20260818,
        )
        details.append({
            "model": {
                "key": spec.key,
                "label": spec.label,
                "paper": PAPER_REFERENCES[spec.key],
                "portfolio_mode": spec.portfolio_mode,
                "rebalance_days": spec.rebalance_days,
                "top_k": spec.top_k if spec.portfolio_mode == "cross_section" else None,
            },
            "full_sample": _compact_metrics(full["metrics"]),
            "holdout": {
                "start": holdout[0].strftime("%Y-%m-%d"),
                "end": holdout[-1].strftime("%Y-%m-%d"),
                "metrics": _compact_metrics(holdout_result["metrics"]),
                "buy_hold_return_pct": round(holdout_base_return, 4),
                "excess_vs_buy_hold_pp": round(holdout_return - holdout_base_return, 4),
            },
            "development_windows": window_rows,
            "robustness": {
                "median_development_excess_pp": round(float(np.median(development_excess)), 4) if development_excess else 0.0,
                "worst_development_return_pct": round(min(development_returns), 4) if development_returns else 0.0,
                "beat_buy_hold_windows": sum(value > 0.0 for value in development_excess),
                "development_window_count": len(development_excess),
                "anchor_annualized_returns_pct": [round(value, 4) for value in anchor_returns],
                "anchor_return_std_pp": round(float(np.std(anchor_returns, ddof=0)), 4) if anchor_returns else 0.0,
                "paired_block_bootstrap_vs_buy_hold": bootstrap,
            },
            "cost_stress": {
                "one_way_fee_rate": stress_fee_rate,
                "metrics": _compact_metrics(stressed["metrics"]),
            },
            "audit": {
                "trade_count": len(full["trades"]),
                "trades": full["trades"],
                "equity_curve": full["equity_curve"],
            },
        })

    ranking_frame = pd.DataFrame.from_records([
        {
            "key": row["model"]["key"],
            "holdout_excess": row["holdout"]["excess_vs_buy_hold_pp"],
            "median_development_excess": row["robustness"]["median_development_excess_pp"],
            "full_sortino": row["full_sample"]["sortino_ratio"],
            "max_drawdown": row["full_sample"]["max_drawdown_pct"],
            "stress_return": row["cost_stress"]["metrics"]["total_return_pct"],
            "turnover": row["full_sample"]["turnover_pct"],
            "anchor_std": row["robustness"]["anchor_return_std_pp"],
            "full_return": row["full_sample"]["total_return_pct"],
        }
        for row in details
    ]).set_index("key")
    rank_columns = {
        "holdout_excess": False,
        "median_development_excess": False,
        "full_sortino": False,
        "max_drawdown": False,
        "stress_return": False,
        "turnover": True,
        "anchor_std": True,
    }
    for column, ascending in rank_columns.items():
        ranking_frame[f"rank_{column}"] = ranking_frame[column].rank(method="average", ascending=ascending)
    rank_names = [f"rank_{column}" for column in rank_columns]
    ranking_frame["aggregate_rank_score"] = ranking_frame[rank_names].mean(axis=1)
    ranking_frame = ranking_frame.sort_values(["aggregate_rank_score", "full_return"], ascending=[True, False])
    detail_by_key = {row["model"]["key"]: row for row in details}
    ranking: List[Dict[str, Any]] = []
    for position, (key, values) in enumerate(ranking_frame.iterrows(), start=1):
        detail = detail_by_key[key]
        required_window_wins = math.ceil(detail["robustness"]["development_window_count"] / 2.0)
        robust_eligible = (
            detail["holdout"]["excess_vs_buy_hold_pp"] > 0.0
            and detail["robustness"]["median_development_excess_pp"] > 0.0
            and detail["robustness"]["beat_buy_hold_windows"] >= required_window_wins
            and detail["full_sample"]["average_exposure_pct"] >= 20.0
            and detail["full_sample"]["max_drawdown_pct"]
            >= baseline_full["metrics"]["max_drawdown_pct"]
        )
        ranking.append({
            "rank": position,
            "model": detail["model"],
            "robust_eligible": robust_eligible,
            "aggregate_rank_score": round(_safe_float(values["aggregate_rank_score"]), 6),
            "criterion_ranks": {
                column: round(_safe_float(values[f"rank_{column}"]), 4)
                for column in rank_columns
            },
            "full_sample": detail["full_sample"],
            "holdout": detail["holdout"],
            "robustness": detail["robustness"],
            "cost_stress": detail["cost_stress"],
        })

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "period": {
            "start": start, "end": end,
            "holdout_start": holdout[0].strftime("%Y-%m-%d"),
            "holdout_end": holdout[-1].strftime("%Y-%m-%d"),
        },
        "universe": {
            "market": "Hong Kong",
            "requested_current_constituent_count": len(snapshot.get("requested_symbols") or []),
            "backtested_symbol_count": len(histories),
            "symbols": list(histories),
            "selection_rule": "All 93 HSI constituents captured on 2026-08-18 with at least the configured minimum number of bars.",
            "survivorship_bias": True,
        },
        "data_snapshot": {
            "path": str(DEFAULT_SNAPSHOT_PATH.relative_to(ROOT)),
            "as_of_date": snapshot.get("as_of_date"),
            "captured_at": snapshot.get("captured_at"),
            "source": snapshot.get("source"),
            "manifest": {symbol: snapshot.get("manifest", {}).get(symbol) for symbol in histories},
            "fetch_errors": snapshot.get("fetch_errors"),
        },
        "methodology": {
            "signal_delay": "Signals use close data through T and execute at T+1 open.",
            "base_one_way_fee_rate": fee_rate,
            "stress_one_way_fee_rate": stress_fee_rate,
            "integer_share_approximation": True,
            "hong_kong_t_plus_zero": True,
            "max_operations_per_symbol_per_day": 1,
            "exact_signal_portfolio_adaptation": "Equal initial capital sleeves; symbols remain independent, but current HSI weights are deliberately not backfilled.",
            "cross_section_portfolio": "Top 4, inverse 20-day volatility, 95% gross exposure, 35% single-name cap, 20-day rebalance.",
            "ranking": "Equal-weight mean ordinal rank across seven pre-declared robustness criteria; lower is better.",
            "ranking_criteria": list(rank_columns),
            "parameters_tuned_on_test_period": False,
        },
        "benchmarks": {
            "equal_weight_buy_hold": _compact_metrics(baseline_full["metrics"]),
            "hsi_buy_hold": _compact_metrics(benchmark_full["metrics"]),
            **naive_benchmarks,
        },
        "development_windows": [
            {"start": left.strftime("%Y-%m-%d"), "end": right.strftime("%Y-%m-%d")}
            for left, right in development_windows
        ],
        "top_three": ranking[:3],
        "validated_top_three": [row for row in ranking if row["robust_eligible"]][:3],
        "validation_gate": {
            "description": "Positive holdout excess, positive median development excess, wins at least half of development windows, at least 20% average exposure, and full-sample drawdown no worse than equal-weight buy-and-hold.",
            "passed_model_count": sum(row["robust_eligible"] for row in ranking),
            "relative_ranking_is_not_deployment_approval": True,
        },
        "ranking": ranking,
        "candidate_details": details,
        "limitations": [
            "The universe uses current constituents to look backward and therefore has survivorship bias.",
            "Tencent supplies at most 800 daily bars, so this is a recent-regime test rather than a full-cycle replication.",
            "Board-lot history is unavailable; integer shares are used to avoid introducing present-day lot-size bias.",
            "FollowBB, RideMood, and 5/60 trend use exact signal rules, but their portfolios are equal-weight adaptations rather than HSI-weight replications.",
            "Selecting the best three from seven candidates still creates model-selection bias even with a final holdout.",
            "No backtest establishes future profitability; confidence intervals can include zero even for a highly ranked model.",
        ],
    }


def render_markdown(result: Mapping[str, Any]) -> str:
    passed = int(result["validation_gate"]["passed_model_count"])
    lines = [
        "# Hong Kong arXiv model comparison",
        "",
        f"Period: {result['period']['start']} to {result['period']['end']}; holdout: {result['period']['holdout_start']} to {result['period']['holdout_end']}.",
        f"Universe: {result['universe']['backtested_symbol_count']} current HSI constituents (survivorship-biased).",
        f"Validation gate: {passed} model(s) passed. The table below is a relative ranking, not deployment approval.",
        "",
        "## Top three",
        "",
        "| Rank | Model | Fidelity | Return | Max drawdown | Sortino | Holdout excess | Turnover |",
        "|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in result["top_three"]:
        paper = row["model"]["paper"]
        lines.append(
            f"| {row['rank']} | {row['model']['label']} | {paper['fidelity']} | "
            f"{row['full_sample']['total_return_pct']:.2f}% | {row['full_sample']['max_drawdown_pct']:.2f}% | "
            f"{row['full_sample']['sortino_ratio']:.2f} | {row['holdout']['excess_vs_buy_hold_pp']:.2f} pp | "
            f"{row['full_sample']['turnover_pct']:.1f}% |"
        )
    lines.extend([
        "",
        "## Benchmarks",
        "",
        f"- Equal-weight buy and hold: {result['benchmarks']['equal_weight_buy_hold']['total_return_pct']:.2f}% return, "
        f"{result['benchmarks']['equal_weight_buy_hold']['max_drawdown_pct']:.2f}% max drawdown.",
        f"- HSI buy and hold: {result['benchmarks']['hsi_buy_hold']['total_return_pct']:.2f}% return, "
        f"{result['benchmarks']['hsi_buy_hold']['max_drawdown_pct']:.2f}% max drawdown.",
        f"- Naive 60-day momentum: {result['benchmarks']['naive_momentum_60']['full_sample']['total_return_pct']:.2f}% full return, "
        f"{result['benchmarks']['naive_momentum_60']['holdout']['total_return_pct']:.2f}% holdout return.",
        f"- Naive 120-day momentum: {result['benchmarks']['naive_momentum_120']['full_sample']['total_return_pct']:.2f}% full return, "
        f"{result['benchmarks']['naive_momentum_120']['holdout']['total_return_pct']:.2f}% holdout return.",
        "",
        "## Ranking method",
        "",
        "The score is the equal-weight mean ordinal rank across holdout excess return, median development-window excess, "
        "full-sample Sortino, maximum drawdown, stressed-fee return, turnover, and start-anchor stability. Lower is better.",
        "A model is validated only when holdout and median development excess are positive, at least half of development windows beat buy-and-hold, average exposure is at least 20%, and full-sample drawdown is no worse than buy-and-hold.",
        "",
        "## Important limits",
        "",
    ])
    lines.extend(f"- {item}" for item in result["limitations"])
    lines.append("")
    return "\n".join(lines)


def _eligible_histories(
    histories: Mapping[str, pd.DataFrame],
    *,
    minimum_bars: int,
    end: date,
) -> Dict[str, pd.DataFrame]:
    return {
        symbol: frame[frame.index.date <= end].copy()
        for symbol, frame in histories.items()
        if len(frame[frame.index.date <= end]) >= minimum_bars
    }


def _default_evaluation_start(histories: Mapping[str, pd.DataFrame], warmup_bars: int) -> str:
    starts = [frame.index[warmup_bars - 1] for frame in histories.values() if len(frame) >= warmup_bars]
    if len(starts) != len(histories) or not starts:
        raise ValueError("Not every eligible history has enough warmup bars")
    return max(starts).strftime("%Y-%m-%d")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(DEFAULT_HK_UNIVERSE))
    parser.add_argument("--as-of", default=_default_completed_hk_date().isoformat())
    parser.add_argument("--start", default="")
    parser.add_argument("--minimum-bars", type=int, default=650)
    parser.add_argument("--warmup-bars", type=int, default=130)
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--fee-rate", type=float, default=0.002)
    parser.add_argument("--stress-fee-rate", type=float, default=0.003)
    parser.add_argument("--holdout-days", type=int, default=126)
    parser.add_argument("--window-days", type=int, default=126)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_PATH)
    parser.add_argument("--refresh-snapshot", action="store_true")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of)
    symbols = [item.strip().zfill(5) for item in args.symbols.split(",") if item.strip()]
    if args.refresh_snapshot or not args.snapshot.exists():
        histories, benchmark, snapshot = create_market_snapshot(symbols, as_of=as_of, path=args.snapshot)
    else:
        histories, benchmark, snapshot = load_market_snapshot(args.snapshot, expected_as_of=as_of)
    eligible = _eligible_histories(histories, minimum_bars=args.minimum_bars, end=as_of)
    if len(eligible) < 10:
        raise RuntimeError(f"Only {len(eligible)} symbols have at least {args.minimum_bars} bars")
    start = args.start or _default_evaluation_start(eligible, args.warmup_bars)
    result = compare_models(
        histories=eligible,
        benchmark=benchmark,
        snapshot=snapshot,
        start=start,
        end=as_of.isoformat(),
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        stress_fee_rate=args.stress_fee_rate,
        holdout_days=args.holdout_days,
        window_days=args.window_days,
    )
    result["data_snapshot"]["path"] = str(args.snapshot.resolve())
    result["configuration"] = {
        "minimum_bars": args.minimum_bars,
        "warmup_bars": args.warmup_bars,
        "initial_cash_hkd": args.initial_cash,
    }
    atomic_write_json(args.out, result)
    atomic_write_text(args.markdown_out, render_markdown(result), encoding="utf-8")
    print({
        "output": str(args.out.resolve()),
        "markdown": str(args.markdown_out.resolve()),
        "period": result["period"],
        "universe": result["universe"],
        "benchmarks": result["benchmarks"],
        "top_three": [
            {
                "rank": row["rank"],
                "model": row["model"]["label"],
                "return_pct": row["full_sample"]["total_return_pct"],
                "max_drawdown_pct": row["full_sample"]["max_drawdown_pct"],
                "sortino": row["full_sample"]["sortino_ratio"],
                "holdout_excess_pp": row["holdout"]["excess_vs_buy_hold_pp"],
            }
            for row in result["top_three"]
        ],
    })


if __name__ == "__main__":
    main()
