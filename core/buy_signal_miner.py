"""Historical buy-signal mining for A-share selection and monitor summaries.

This module is deliberately deterministic. It scans historical OHLCV bars for
technical setups that would have been visible at the close of each signal day,
then evaluates the following 5/10/20 trading-day paths. The output is meant to
answer a user-facing question: "did the system ever detect a buy point, when,
and was that setup historically useful?"
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import pandas as pd


@dataclass(frozen=True)
class BuySignalForwardWindow:
    horizon_days: int
    return_pct: Optional[float]
    max_runup_pct: Optional[float]
    max_drawdown_pct: Optional[float]


@dataclass(frozen=True)
class HistoricalBuySignal:
    trade_date: str
    signal_type: str
    signal_label: str
    close: float
    score: float
    volume_ratio: Optional[float]
    forward_windows: List[BuySignalForwardWindow]
    note: str


@dataclass(frozen=True)
class BuySignalBacktestSummary:
    symbol: str
    sample_size: int
    latest_signal: Optional[HistoricalBuySignal]
    latest_signal_age_days: Optional[int]
    hit_rate_5d: Optional[float]
    avg_return_5d_pct: Optional[float]
    avg_return_10d_pct: Optional[float]
    avg_return_20d_pct: Optional[float]
    expected_score: float
    confidence: str
    warning: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def mine_historical_buy_signals(
    symbol: str,
    df: Optional[pd.DataFrame],
    *,
    trade_date: Optional[str] = None,
    max_lookback_bars: int = 260,
    horizons: tuple[int, ...] = (5, 10, 20),
) -> BuySignalBacktestSummary:
    """Find historical buy setups and summarize their forward performance."""
    data = _normalize_ohlcv(df)
    if trade_date:
        data = data[data.index <= pd.to_datetime(trade_date)]
    if not data.empty and max_lookback_bars > 0:
        data = data.tail(max_lookback_bars)
    if data.empty or len(data) < 45:
        return _empty_summary(symbol, "历史K线不足，无法回测买点")

    signals: List[HistoricalBuySignal] = []
    # Include the latest bar so the selector can proactively say "today a buy
    # signal appeared"; its forward windows will be None until future bars exist.
    last_origin = len(data) - 1
    for idx in range(25, last_origin + 1):
        signal = _detect_signal(data, idx=idx, horizons=horizons)
        if signal is not None:
            signals.append(signal)

    if not signals:
        return BuySignalBacktestSummary(
            symbol=symbol.upper(),
            sample_size=0,
            latest_signal=None,
            latest_signal_age_days=None,
            hit_rate_5d=None,
            avg_return_5d_pct=None,
            avg_return_10d_pct=None,
            avg_return_20d_pct=None,
            expected_score=45.0,
            confidence="low",
            warning="过去样本中没有检测到足够清晰的买点，当前只能按观察处理",
        )

    latest = max(signals, key=lambda item: item.trade_date)
    latest_idx = data.index.get_loc(pd.to_datetime(latest.trade_date))
    latest_age = max(0, len(data) - 1 - int(latest_idx))
    returns_by_horizon: Dict[int, List[float]] = {h: [] for h in horizons}
    for signal in signals:
        for window in signal.forward_windows:
            if window.return_pct is not None:
                returns_by_horizon.setdefault(window.horizon_days, []).append(window.return_pct)

    ret5 = returns_by_horizon.get(5, [])
    ret10 = returns_by_horizon.get(10, [])
    ret20 = returns_by_horizon.get(20, [])
    hit_rate_5d = None if not ret5 else sum(1 for value in ret5 if value > 0) / len(ret5)
    avg5 = _mean(ret5)
    avg10 = _mean(ret10)
    avg20 = _mean(ret20)
    score = 50.0
    if hit_rate_5d is not None:
        score += (hit_rate_5d - 0.5) * 45.0
    if avg5 is not None:
        score += _bounded(avg5 * 4.0, -18.0, 22.0)
    if avg20 is not None:
        score += _bounded(avg20 * 1.6, -14.0, 18.0)
    score += _bounded((latest.score - 60.0) * 0.35, -8.0, 12.0)
    if latest_age <= 3:
        score += 8.0
    elif latest_age <= 10:
        score += 3.0
    elif latest_age > 30:
        score -= 6.0
    score = _bounded(score, 0.0, 100.0)

    if len(signals) < 4:
        confidence = "low"
    elif score >= 70 and (hit_rate_5d or 0.0) >= 0.55:
        confidence = "high"
    elif score >= 55:
        confidence = "medium"
    else:
        confidence = "low"

    warning = ""
    latest_20 = _window_return(latest, 20)
    latest_5 = _window_return(latest, 5)
    if latest_age > 20:
        warning = "最近一次买点距离当前较久，不能当作当下追入依据"
    elif latest_20 is not None and latest_20 > 18:
        warning = "买点后涨幅已经较大，优先等回踩或新突破确认，避免追高"
    elif latest_5 is not None and latest_5 < -4:
        warning = "最近买点后的短线表现偏弱，需要重新确认趋势"
    elif confidence == "low":
        warning = "历史样本不足或胜率一般，只能降低仓位试错"
    else:
        warning = "历史买点有效性尚可，但仍需结合当前价格触发和止损线"

    return BuySignalBacktestSummary(
        symbol=symbol.upper(),
        sample_size=len(signals),
        latest_signal=latest,
        latest_signal_age_days=latest_age,
        hit_rate_5d=None if hit_rate_5d is None else round(hit_rate_5d, 3),
        avg_return_5d_pct=None if avg5 is None else round(avg5, 3),
        avg_return_10d_pct=None if avg10 is None else round(avg10, 3),
        avg_return_20d_pct=None if avg20 is None else round(avg20, 3),
        expected_score=round(score, 2),
        confidence=confidence,
        warning=warning,
    )


def buy_signal_summary_text(summary: Dict[str, Any] | BuySignalBacktestSummary) -> str:
    data = summary.as_dict() if isinstance(summary, BuySignalBacktestSummary) else dict(summary or {})
    latest = data.get("latest_signal") or {}
    if not latest:
        return str(data.get("warning") or "系统回测未发现明确买点")
    date = str(latest.get("trade_date") or "")
    label = str(latest.get("signal_label") or latest.get("signal_type") or "买点")
    hit = data.get("hit_rate_5d")
    avg = data.get("avg_return_5d_pct")
    age = data.get("latest_signal_age_days")
    hit_text = "样本不足" if hit is None else f"5日胜率 {float(hit) * 100:.0f}%"
    avg_text = "" if avg is None else f"，平均 {float(avg):+.1f}%"
    age_text = "" if age is None else f"，距今 {int(age)} 个交易日"
    return f"系统回测信号: {date} 检测到{label}{age_text}；历史{hit_text}{avg_text}"


def _detect_signal(
    data: pd.DataFrame,
    *,
    idx: int,
    horizons: tuple[int, ...],
) -> Optional[HistoricalBuySignal]:
    row = data.iloc[idx]
    prev = data.iloc[idx - 1]
    close = float(row["Close"])
    high = float(row["High"])
    low = float(row["Low"])
    prev_close = float(prev["Close"])
    if close <= 0 or prev_close <= 0:
        return None

    prev_window = data.iloc[:idx]
    high20 = float(prev_window["High"].tail(20).max())
    ma5 = float(data["Close"].iloc[: idx + 1].tail(5).mean())
    ma10 = float(data["Close"].iloc[: idx + 1].tail(10).mean())
    ma20 = float(data["Close"].iloc[: idx + 1].tail(20).mean())
    ma20_prev = float(data["Close"].iloc[:idx].tail(20).mean())
    volume = float(row.get("Volume", 0.0) or 0.0)
    vol_ma5 = float(prev_window["Volume"].tail(5).mean()) if "Volume" in prev_window else 0.0
    volume_ratio = volume / vol_ma5 if vol_ma5 > 0 else None
    close_position = (close - low) / (high - low) if high > low else 0.5
    change_pct = (close / prev_close - 1.0) * 100.0
    ma20_slope = (ma20 / ma20_prev - 1.0) * 100.0 if ma20_prev > 0 else 0.0

    signal_type = ""
    label = ""
    score = 0.0
    note_parts: List[str] = []

    if close > high20 and (volume_ratio or 0.0) >= 1.18 and close_position >= 0.62:
        signal_type = "volume_breakout"
        label = "放量突破买点"
        score = 64.0
        score += _bounded(((volume_ratio or 1.0) - 1.0) * 12.0, 0.0, 18.0)
        score += _bounded((close_position - 0.62) * 25.0, 0.0, 10.0)
        score += _bounded(ma20_slope * 6.0, -6.0, 10.0)
        note_parts.append("收盘突破20日高点")
        note_parts.append("量能放大")
    else:
        near_ma20 = low <= ma20 * 1.015 and close >= ma20 * 0.995
        reclaimed = close > ma5 and close > prev_close and close_position >= 0.58
        trend_ok = ma5 >= ma10 >= ma20 * 0.985 or ma20_slope > 0
        if near_ma20 and reclaimed and trend_ok and (volume_ratio or 1.0) >= 0.85:
            signal_type = "pullback_reversal"
            label = "回踩反转买点"
            score = 58.0
            score += _bounded((close_position - 0.58) * 22.0, 0.0, 10.0)
            score += _bounded(change_pct * 2.0, 0.0, 12.0)
            score += _bounded(ma20_slope * 6.0, -5.0, 8.0)
            note_parts.append("回踩20日均线后重新收强")
            note_parts.append("短均线结构未破坏")

    if not signal_type:
        return None

    windows = [_forward_window(data, idx=idx, horizon=horizon, entry=close) for horizon in horizons]
    return HistoricalBuySignal(
        trade_date=pd.to_datetime(data.index[idx]).strftime("%Y-%m-%d"),
        signal_type=signal_type,
        signal_label=label,
        close=round(close, 4),
        score=round(_bounded(score, 0.0, 100.0), 2),
        volume_ratio=None if volume_ratio is None else round(volume_ratio, 3),
        forward_windows=windows,
        note="；".join(note_parts),
    )


def _forward_window(data: pd.DataFrame, *, idx: int, horizon: int, entry: float) -> BuySignalForwardWindow:
    end = idx + horizon
    if entry <= 0 or end >= len(data):
        return BuySignalForwardWindow(horizon_days=horizon, return_pct=None, max_runup_pct=None, max_drawdown_pct=None)
    future = data.iloc[idx + 1 : end + 1]
    exit_price = float(data["Close"].iloc[end])
    high = float(future["High"].max())
    low = float(future["Low"].min())
    return BuySignalForwardWindow(
        horizon_days=horizon,
        return_pct=round((exit_price / entry - 1.0) * 100.0, 3),
        max_runup_pct=round((high / entry - 1.0) * 100.0, 3),
        max_drawdown_pct=round((low / entry - 1.0) * 100.0, 3),
    )


def _window_return(signal: HistoricalBuySignal, horizon: int) -> Optional[float]:
    for window in signal.forward_windows:
        if window.horizon_days == horizon:
            return window.return_pct
    return None


def _empty_summary(symbol: str, warning: str) -> BuySignalBacktestSummary:
    return BuySignalBacktestSummary(
        symbol=symbol.upper(),
        sample_size=0,
        latest_signal=None,
        latest_signal_age_days=None,
        hit_rate_5d=None,
        avg_return_5d_pct=None,
        avg_return_10d_pct=None,
        avg_return_20d_pct=None,
        expected_score=45.0,
        confidence="low",
        warning=warning,
    )


def _normalize_ohlcv(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    rename = {col: str(col).capitalize() for col in out.columns if str(col).lower() in {"open", "high", "low", "close", "volume"}}
    out = out.rename(columns=rename)
    if "Open" not in out.columns and "Close" in out.columns:
        out["Open"] = pd.to_numeric(out["Close"], errors="coerce").shift(1)
        out["Open"] = out["Open"].fillna(pd.to_numeric(out["Close"], errors="coerce"))
    for col in ["Open", "High", "Low", "Close"]:
        if col not in out.columns:
            return pd.DataFrame()
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    out["Volume"] = pd.to_numeric(out["Volume"], errors="coerce").fillna(0.0)
    return out.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _bounded(value: float, lo: float, hi: float) -> float:
    if math.isnan(value):
        return lo
    return max(lo, min(hi, value))


__all__ = [
    "BuySignalBacktestSummary",
    "BuySignalForwardWindow",
    "HistoricalBuySignal",
    "buy_signal_summary_text",
    "mine_historical_buy_signals",
]
