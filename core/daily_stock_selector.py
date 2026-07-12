"""Daily A-share stock selection model.

The selector is deterministic and LLM-free. It combines three information
layers for each trading day:

1. domestic news/theme hits;
2. sector-to-leader mapping from the news enrichment layer;
3. big-money sector flow and constituent flow;
4. each candidate stock's daily OHLCV behavior plus fundamental quality.

The output is meant to answer: which A-share sectors are hot today, which
stocks deserve attention, why, and what the day's tape is saying.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

import math
import pandas as pd

from core.buy_signal_miner import BuySignalBacktestSummary, mine_historical_buy_signals
from core.ashare_behavioral_factor import assess_behavioral_universe
from services.news_sources import RawNewsItem


@dataclass(frozen=True)
class TapeAnalysis:
    trade_date: str
    close: float
    change_pct: float
    intraday_range_pct: float
    close_position: float
    volume_ratio: Optional[float]
    ma5_slope_pct: Optional[float]
    ma20_gap_pct: Optional[float]
    limit_up_like: bool
    breakout_20d: bool
    breakdown_20d: bool
    interpretation: str
    tape_score: float


@dataclass(frozen=True)
class TrendFrame:
    horizon: str
    direction: str
    score: float
    change_pct: Optional[float]
    ma_gap_pct: Optional[float]
    breakout: bool
    breakdown: bool
    interpretation: str


@dataclass(frozen=True)
class MultiPeriodTrend:
    daily: TrendFrame
    weekly: TrendFrame
    monthly: TrendFrame
    alignment: str
    alignment_score: float
    interpretation: str


@dataclass(frozen=True)
class Affordability:
    available_cash_cny: Optional[float]
    lot_size: int
    min_lot_cash: Optional[float]
    max_lots: Optional[int]
    affordable: Optional[bool]
    note: str


@dataclass(frozen=True)
class EntryPlan:
    action: str
    setup: str
    trigger_price: Optional[float]
    pullback_zone: Optional[List[float]]
    invalidation_price: Optional[float]
    stop_loss_price: Optional[float]
    chase_risk: str
    note: str


@dataclass(frozen=True)
class PathWindow:
    horizon_days: int
    sample_size: int
    mean_return_pct: float
    median_return_pct: float
    q20_return_pct: float
    q80_return_pct: float
    win_probability: float
    drawdown_probability: float
    max_runup_pct: float
    max_drawdown_pct: float


@dataclass(frozen=True)
class PathDistribution:
    windows: List[PathWindow]
    path_shape: str
    path_score: float
    interpretation: str


@dataclass(frozen=True)
class RiskDefense:
    atr_pct: float
    atr_shock_ratio: Optional[float]
    downside_tail_pct: Optional[float]
    risk_level: str
    risk_penalty: float
    note: str


@dataclass(frozen=True)
class CalibrationSnapshot:
    sample_size: int
    hit_rate: Optional[float]
    avg_forward_return_pct: Optional[float]
    confidence_multiplier: float
    note: str


@dataclass(frozen=True)
class SectorSelection:
    sector: str
    heat_score: float
    news_count: int
    avg_news_impact: float
    risk_penalty: float
    tape_confirm_score: float = 0.0
    confirmed_stock_count: int = 0
    fund_flow_score: float = 0.0
    main_net_inflow_cny: Optional[float] = None
    fund_flow_rank: Optional[int] = None
    evidence_titles: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class StockSelection:
    symbol: str
    name: str
    sector: str
    score: float
    attention: str
    news_score: float
    tape_score: float
    risk_penalty: float
    reasons: List[str]
    evidence_titles: List[str]
    tape: TapeAnalysis
    trend: MultiPeriodTrend
    affordability: Affordability
    entry_plan: EntryPlan
    path_distribution: PathDistribution
    risk_defense: RiskDefense
    calibration: CalibrationSnapshot
    buy_signal_backtest: BuySignalBacktestSummary
    model_edge_score: float
    fundamental_score: float = 50.0
    fundamental_model: str = ""
    money_flow_score: float = 0.0
    behavioral_factor_score: float = 0.0
    behavioral_target_weight_pct: float = 0.0
    behavioral_selected: bool = False
    behavioral_low_confidence: bool = True


@dataclass(frozen=True)
class DailySelectionResult:
    trade_date: str
    sectors: List[SectorSelection]
    stocks: List[StockSelection]
    reference_pool: List[StockSelection]  # 板块龙头参考池（大市值稳定标的）
    action_pool: List[StockSelection]     # 异动股实战池（技术异动/高波动）
    news_impact_summary: Dict[str, Any]


def build_daily_selection(
    items: Iterable[RawNewsItem],
    history_by_symbol: Dict[str, pd.DataFrame],
    *,
    trade_date: Optional[str] = None,
    max_sectors: int = 8,
    max_stocks: int = 20,
    available_cash_cny: Optional[float] = None,
    lot_size: int = 100,
    sector_fund_flows: Optional[List[Dict[str, Any]]] = None,
    fundamentals_by_symbol: Optional[Dict[str, Dict[str, Any]]] = None,
) -> DailySelectionResult:
    """Rank hot A-share sectors and watchlist stocks for a single day."""
    news_rows = _collect_news_rows(items)
    flow_rows = _normalize_sector_fund_flows(sector_fund_flows or [])
    sector_rows = _rank_sectors(news_rows, flows=flow_rows, max_sectors=max_sectors)

    stock_buckets: Dict[str, Dict[str, Any]] = {}
    sector_heat = {row.sector: row.heat_score for row in sector_rows}
    flow_by_sector = {row["sector"]: row for row in flow_rows}
    flow_by_symbol: Dict[str, Dict[str, Any]] = {}
    behavioral_assessments = assess_behavioral_universe(
        history_by_symbol,
        as_of=trade_date,
    )
    for flow in flow_rows:
        for leader in flow.get("leaders") or []:
            symbol = str(leader.get("symbol") or "").strip()
            if symbol:
                flow_by_symbol[symbol] = {**flow, "leader": leader}
    for row in news_rows:
        for leader in row["leaders"]:
            symbol = str(leader.get("symbol") or "").strip()
            if not _is_a_share(symbol):
                continue
            bucket = stock_buckets.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "name": str(leader.get("name") or symbol),
                    "sector": row["sector"],
                    "news_score": 0.0,
                    "risk_penalty": 0.0,
                    "evidence_titles": [],
                    "reasons": [],
                },
            )
            impact = float(row["news_impact"])
            bucket["news_score"] += max(0.0, impact) + sector_heat.get(row["sector"], 0.0) * 0.25
            bucket["risk_penalty"] += max(0.0, -impact)
            _append_unique(bucket["evidence_titles"], row["title"], limit=5)
            reason = str(leader.get("reason") or "")
            if reason:
                _append_unique(bucket["reasons"], reason, limit=5)

    for flow in flow_rows:
        sector = flow["sector"]
        for leader in flow.get("leaders") or []:
            symbol = str(leader.get("symbol") or "").strip()
            if not _is_a_share(symbol):
                continue
            bucket = stock_buckets.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "name": str(leader.get("name") or symbol),
                    "sector": sector,
                    "news_score": 0.0,
                    "risk_penalty": 0.0,
                    "evidence_titles": [],
                    "reasons": [],
                },
            )
            money_score = _bounded(_safe_float(flow.get("fund_flow_score")), 0.0, 100.0)
            leader_score = _bounded(_safe_float(leader.get("money_flow_score")), 0.0, 100.0)
            bucket["news_score"] += money_score * 0.45 + leader_score * 0.35
            _append_unique(bucket["evidence_titles"], f"{sector}主力资金净流入靠前", limit=5)
            _append_unique(bucket["reasons"], f"{sector}板块大资金流入强，板块内资金排名靠前", limit=5)

    stocks: List[StockSelection] = []
    _tapes: Dict[str, Any] = {}
    affordability_filtered = 0
    for symbol, bucket in stock_buckets.items():
        tape = analyze_daily_tape(history_by_symbol.get(symbol), trade_date=trade_date)
        if tape is None:
            continue
        _tapes[symbol] = tape
        trend = analyze_multi_period_trend(history_by_symbol.get(symbol), trade_date=trade_date)
        if trend is None:
            continue
        affordability = build_affordability(tape.close, available_cash_cny=available_cash_cny, lot_size=lot_size)
        if affordability.affordable is False:
            affordability_filtered += 1
            continue
        news_score = _bounded(bucket["news_score"], 0.0, 100.0)
        risk_penalty = _bounded(bucket["risk_penalty"], 0.0, 100.0)
        fundamental = (fundamentals_by_symbol or {}).get(symbol) or {}
        fundamental_score = _bounded(_safe_float(fundamental.get("score"), 50.0), 0.0, 100.0)
        money_flow_score = _bounded(_safe_float((flow_by_symbol.get(symbol) or {}).get("fund_flow_score")), 0.0, 100.0)
        path_distribution = estimate_path_distribution(history_by_symbol.get(symbol), trade_date=trade_date)
        risk_defense = build_risk_defense(history_by_symbol.get(symbol), path_distribution=path_distribution, trade_date=trade_date)
        calibration = build_walk_forward_calibration(
            history_by_symbol.get(symbol),
            tape=tape,
            trend=trend,
            trade_date=trade_date,
        )
        buy_signal_backtest = mine_historical_buy_signals(
            symbol,
            history_by_symbol.get(symbol),
            trade_date=trade_date,
        )
        model_edge_score = _bounded(
            path_distribution.path_score * 0.42
            + (100.0 - risk_defense.risk_penalty) * 0.28
            + calibration.confidence_multiplier * 100.0 * 0.22
            + buy_signal_backtest.expected_score * 0.08,
            0.0,
            100.0,
        )
        behavioral = behavioral_assessments.get(symbol)
        if behavioral is not None and not behavioral.low_confidence:
            # The validated A-share factor is the deterministic ranking base.
            # News, sector flow and fundamentals remain independent evidence;
            # short-term tape has only a small timing role.
            score = _bounded(
                behavioral.score * 0.40
                + news_score * 0.18
                + fundamental_score * 0.14
                + money_flow_score * 0.14
                + model_edge_score * 0.08
                + tape.tape_score * 0.06
                - risk_penalty * 0.25
                - risk_defense.risk_penalty * 0.10,
                0.0,
                100.0,
            )
        else:
            score = _bounded(
                news_score * 0.34
                + tape.tape_score * 0.20
                + trend.alignment_score * 0.14
                + fundamental_score * 0.14
                + money_flow_score * 0.16
                + model_edge_score * 0.12
                - risk_penalty * 0.30
                - risk_defense.risk_penalty * 0.12,
                0.0,
                100.0,
            )
        reasons = list(bucket["reasons"])
        if behavioral is not None:
            reasons.append(
                f"A股行为因子{behavioral.score:.1f}分，"
                f"{'入选目标组合' if behavioral.selected else '未进入前四'}，"
                f"20日经验收益{behavioral.expected_return_pct:+.2f}%"
            )
        reasons.append(tape.interpretation)
        reasons.append(trend.interpretation)
        if fundamental.get("reason"):
            reasons.append(str(fundamental.get("reason")))
        reasons.append(path_distribution.interpretation)
        reasons.append(risk_defense.note)
        reasons.append(calibration.note)
        if buy_signal_backtest.latest_signal is not None:
            latest = buy_signal_backtest.latest_signal
            hit = buy_signal_backtest.hit_rate_5d
            hit_text = "样本不足" if hit is None else f"5日胜率{hit:.0%}"
            reasons.append(f"系统回测信号: {latest.trade_date}出现{latest.signal_label}，历史{hit_text}")
        elif buy_signal_backtest.warning:
            reasons.append(buy_signal_backtest.warning)
        entry_plan = build_entry_plan(history_by_symbol.get(symbol), tape=tape, trade_date=trade_date)
        stocks.append(
            StockSelection(
                symbol=symbol,
                name=bucket["name"],
                sector=bucket["sector"],
                score=round(score, 2),
                attention=_attention_label(score, tape, risk_penalty),
                news_score=round(news_score, 2),
                tape_score=round(tape.tape_score, 2),
                risk_penalty=round(risk_penalty, 2),
                reasons=reasons[:6],
                evidence_titles=list(bucket["evidence_titles"]),
                tape=tape,
                trend=trend,
                affordability=affordability,
                entry_plan=entry_plan,
                path_distribution=path_distribution,
                risk_defense=risk_defense,
                calibration=calibration,
                buy_signal_backtest=buy_signal_backtest,
                model_edge_score=round(model_edge_score, 2),
                fundamental_score=round(fundamental_score, 2),
                fundamental_model=str(fundamental.get("model") or ""),
                money_flow_score=round(money_flow_score, 2),
                behavioral_factor_score=round(behavioral.score, 2) if behavioral else 0.0,
                behavioral_target_weight_pct=round(behavioral.target_weight_pct, 2) if behavioral else 0.0,
                behavioral_selected=bool(behavioral and behavioral.selected),
                behavioral_low_confidence=bool(behavioral is None or behavioral.low_confidence),
            )
        )

    stocks.sort(key=lambda row: (-row.score, row.symbol))
    sector_rows = _rank_sectors(news_rows, stocks=stocks, flows=flow_rows, max_sectors=max_sectors)

    # 双池架构：参考池（板块龙头大市值）+ 实战池（异动高波动）
    reference_pool = []
    action_pool = []
    for s in stocks[:max_stocks]:
        tape = _tapes.get(s.symbol)
        is_leader = any(
            leader.get("symbol") == s.symbol
            for sector in sector_rows
            for leader in (sector.leaders if hasattr(sector, 'leaders') else [])
        )
        if tape and hasattr(tape, 'change_pct') and abs(tape.change_pct) >= 5.0:
            action_pool.append(s)
        elif is_leader or (s.fundamental_score and s.fundamental_score >= 60):
            reference_pool.append(s)
        else:
            action_pool.append(s)

    return DailySelectionResult(
        trade_date=trade_date or _infer_latest_date(history_by_symbol),
        sectors=sector_rows,
        stocks=stocks[:max_stocks],
        reference_pool=reference_pool[:10],
        action_pool=action_pool[:10],
        news_impact_summary={
            **_summarize_news_impact(news_rows),
            "affordability_filtered": affordability_filtered,
            "cash_constraint_enabled": available_cash_cny is not None,
            "fund_flow_sector_count": len(flow_rows),
        },
    )


def analyze_daily_tape(df: Optional[pd.DataFrame], *, trade_date: Optional[str] = None) -> Optional[TapeAnalysis]:
    """Explain a stock's daily OHLCV structure."""
    data = _normalize_ohlcv(df)
    if data.empty or len(data) < 2:
        return None
    if trade_date:
        cutoff = pd.to_datetime(trade_date)
        data = data[data.index <= cutoff]
    if data.empty or len(data) < 2:
        return None

    row = data.iloc[-1]
    prev = data.iloc[-2]
    close = float(row["Close"])
    prev_close = float(prev["Close"])
    open_price = float(row["Open"])
    high = float(row["High"])
    low = float(row["Low"])
    volume = float(row.get("Volume", 0.0) or 0.0)

    change_pct = (close / prev_close - 1.0) * 100 if prev_close else 0.0
    range_pct = (high / low - 1.0) * 100 if low else 0.0
    close_position = (close - low) / (high - low) if high > low else 0.5
    vol_ma5 = float(data["Volume"].iloc[:-1].tail(5).mean()) if len(data) > 5 else 0.0
    volume_ratio = (volume / vol_ma5) if vol_ma5 > 0 else None
    ma5 = data["Close"].tail(5).mean()
    ma5_prev = data["Close"].iloc[:-1].tail(5).mean() if len(data) >= 6 else math.nan
    ma20 = data["Close"].tail(20).mean() if len(data) >= 20 else math.nan
    ma5_slope = (ma5 / ma5_prev - 1.0) * 100 if ma5_prev and not math.isnan(ma5_prev) else None
    ma20_gap = (close / ma20 - 1.0) * 100 if ma20 and not math.isnan(ma20) else None
    high20_prev = float(data["High"].iloc[:-1].tail(20).max()) if len(data) >= 21 else float(data["High"].iloc[:-1].max())
    low20_prev = float(data["Low"].iloc[:-1].tail(20).min()) if len(data) >= 21 else float(data["Low"].iloc[:-1].min())
    breakout = close > high20_prev
    breakdown = close < low20_prev
    limit_up_like = change_pct >= 9.5 and close_position >= 0.85

    tape_score = 50.0
    tape_score += _bounded(change_pct * 2.0, -18.0, 24.0)
    tape_score += (close_position - 0.5) * 22.0
    if volume_ratio is not None:
        tape_score += _bounded((volume_ratio - 1.0) * 10.0, -8.0, 18.0)
    if ma5_slope is not None:
        tape_score += _bounded(ma5_slope * 2.0, -8.0, 10.0)
    if ma20_gap is not None:
        tape_score += _bounded(ma20_gap * 0.7, -8.0, 8.0)
    if breakout:
        tape_score += 12.0
    if breakdown:
        tape_score -= 18.0
    if change_pct > 0 and close < open_price and close_position < 0.45:
        tape_score -= 10.0
    tape_score = _bounded(tape_score, 0.0, 100.0)

    interpretation = _interpret_tape(
        change_pct=change_pct,
        close_position=close_position,
        volume_ratio=volume_ratio,
        breakout=breakout,
        breakdown=breakdown,
        limit_up_like=limit_up_like,
        ma5_slope=ma5_slope,
    )
    return TapeAnalysis(
        trade_date=_date_str(data.index[-1]),
        close=round(close, 4),
        change_pct=round(change_pct, 3),
        intraday_range_pct=round(range_pct, 3),
        close_position=round(close_position, 3),
        volume_ratio=None if volume_ratio is None else round(volume_ratio, 3),
        ma5_slope_pct=None if ma5_slope is None else round(ma5_slope, 3),
        ma20_gap_pct=None if ma20_gap is None else round(ma20_gap, 3),
        limit_up_like=limit_up_like,
        breakout_20d=breakout,
        breakdown_20d=breakdown,
        interpretation=interpretation,
        tape_score=round(tape_score, 2),
    )


def analyze_multi_period_trend(
    df: Optional[pd.DataFrame],
    *,
    trade_date: Optional[str] = None,
) -> Optional[MultiPeriodTrend]:
    """Analyze daily, weekly, and monthly trend confirmation explicitly."""
    data = _normalize_ohlcv(df)
    if trade_date:
        data = data[data.index <= pd.to_datetime(trade_date)]
    if data.empty or len(data) < 2:
        return None

    daily = _trend_frame(data, horizon="daily", lookback=20, ma_window=20)
    weekly_df = _resample_ohlcv(data, "W-FRI")
    monthly_df = _resample_ohlcv(data, "ME")
    weekly = _trend_frame(weekly_df, horizon="weekly", lookback=12, ma_window=10)
    monthly = _trend_frame(monthly_df, horizon="monthly", lookback=6, ma_window=6)
    frames = [daily, weekly, monthly]
    avg_score = sum(frame.score for frame in frames) / len(frames)
    positive = sum(1 for frame in frames if frame.direction == "up")
    negative = sum(1 for frame in frames if frame.direction == "down")
    if positive == 3:
        alignment = "bullish_alignment"
        interpretation = "日线、周线、月线三周期共振向上"
    elif negative >= 2:
        alignment = "bearish_pressure"
        interpretation = "至少两个周期偏弱，短线买点需要更严格确认"
    elif positive >= 2:
        alignment = "partial_bullish"
        interpretation = "多周期部分向上，但还不是完整共振"
    else:
        alignment = "mixed"
        interpretation = "多周期方向不一致，优先降低仓位和追高意愿"
    return MultiPeriodTrend(
        daily=daily,
        weekly=weekly,
        monthly=monthly,
        alignment=alignment,
        alignment_score=round(_bounded(avg_score, 0.0, 100.0), 2),
        interpretation=interpretation,
    )


def build_affordability(
    close_price: float,
    *,
    available_cash_cny: Optional[float],
    lot_size: int = 100,
) -> Affordability:
    min_lot_cash = close_price * lot_size if close_price > 0 else None
    if available_cash_cny is None:
        return Affordability(
            available_cash_cny=None,
            lot_size=lot_size,
            min_lot_cash=round(min_lot_cash, 2) if min_lot_cash is not None else None,
            max_lots=None,
            affordable=None,
            note="未提供可用现金，不做一手资金过滤",
        )
    if min_lot_cash is None:
        return Affordability(
            available_cash_cny=round(available_cash_cny, 2),
            lot_size=lot_size,
            min_lot_cash=None,
            max_lots=0,
            affordable=False,
            note="价格无效，不能计算一手资金",
        )
    max_lots = int(available_cash_cny // min_lot_cash)
    affordable = max_lots >= 1
    return Affordability(
        available_cash_cny=round(available_cash_cny, 2),
        lot_size=lot_size,
        min_lot_cash=round(min_lot_cash, 2),
        max_lots=max_lots,
        affordable=affordable,
        note=("资金足够至少买入一手" if affordable else "可用现金不足一手，过滤出推荐列表"),
    )


def build_entry_plan(
    df: Optional[pd.DataFrame],
    *,
    tape: TapeAnalysis,
    trade_date: Optional[str] = None,
) -> EntryPlan:
    """Build a technical entry plan from the latest daily tape.

    It deliberately expresses conditional buy triggers instead of an unconditional
    recommendation. For A-shares, the plan assumes next-day execution after the
    daily signal is known.
    """
    data = _normalize_ohlcv(df)
    if trade_date:
        data = data[data.index <= pd.to_datetime(trade_date)]
    if data.empty or len(data) < 2:
        return EntryPlan(
            action="wait",
            setup="insufficient_data",
            trigger_price=None,
            pullback_zone=None,
            invalidation_price=None,
            stop_loss_price=None,
            chase_risk="unknown",
            note="历史 OHLCV 不足，不能给出可靠技术触发价",
        )

    close = float(data["Close"].iloc[-1])
    high = float(data["High"].iloc[-1])
    low = float(data["Low"].iloc[-1])
    prev_high20 = float(data["High"].iloc[:-1].tail(20).max()) if len(data) > 1 else high
    prev_low10 = float(data["Low"].iloc[:-1].tail(10).min()) if len(data) > 1 else low
    ma5 = float(data["Close"].tail(5).mean())
    ma10 = float(data["Close"].tail(10).mean())
    atr = _atr(data, window=14)
    support = max(prev_low10, close - 1.2 * atr) if atr > 0 else prev_low10
    stop = min(support, close * 0.94)

    if tape.limit_up_like or tape.change_pct >= 7.0:
        trigger = round(high * 1.01, 2)
        pull_low = round(max(ma5, close - 0.8 * atr), 2)
        pull_high = round(max(ma5, close - 0.35 * atr), 2)
        return EntryPlan(
            action="wait_pullback",
            setup="extended_strength",
            trigger_price=trigger,
            pullback_zone=[pull_low, pull_high],
            invalidation_price=round(stop, 2),
            stop_loss_price=round(stop, 2),
            chase_risk="high",
            note="单日涨幅过大，优先等回踩 5 日线/当日实体中部承接；若次日继续放量突破高点再小仓试错",
        )

    if tape.breakout_20d and (tape.volume_ratio or 0) >= 1.3 and tape.close_position >= 0.65:
        trigger = round(max(high, prev_high20) * 1.003, 2)
        pull_low = round(max(ma5, close - 0.7 * atr), 2)
        pull_high = round(close, 2)
        return EntryPlan(
            action="buy_on_breakout_or_pullback",
            setup="volume_breakout",
            trigger_price=trigger,
            pullback_zone=[pull_low, pull_high],
            invalidation_price=round(max(prev_high20 * 0.985, stop), 2),
            stop_loss_price=round(max(prev_high20 * 0.985, stop), 2),
            chase_risk="medium",
            note="放量突破型，次日站上触发价可确认；若不追高，等回踩突破位/5 日线不破再介入",
        )

    if tape.change_pct > 1.5 and tape.close_position >= 0.70 and (tape.volume_ratio or 1.0) >= 1.1:
        pull_low = round(max(ma5, close - 0.9 * atr), 2)
        pull_high = round(max(ma5, close - 0.35 * atr), 2)
        return EntryPlan(
            action="buy_on_pullback",
            setup="strong_close",
            trigger_price=round(high * 1.005, 2),
            pullback_zone=[pull_low, pull_high],
            invalidation_price=round(stop, 2),
            stop_loss_price=round(stop, 2),
            chase_risk="medium",
            note="强势收盘但未有效突破，适合等回踩承接；若次日直接高开过多，不追",
        )

    if tape.change_pct > 0 and tape.close_position < 0.35:
        return EntryPlan(
            action="wait",
            setup="fade_after_rally",
            trigger_price=round(high * 1.01, 2),
            pullback_zone=[round(ma10, 2), round(ma5, 2)] if ma10 <= ma5 else [round(ma5, 2), round(ma10, 2)],
            invalidation_price=round(stop, 2),
            stop_loss_price=round(stop, 2),
            chase_risk="high",
            note="冲高回落，先等重新放量站回当日高点附近；否则只观察不买",
        )

    if tape.breakdown_20d or (tape.change_pct < -2.0 and tape.close_position <= 0.35):
        return EntryPlan(
            action="avoid",
            setup="weak_breakdown",
            trigger_price=round(high * 1.02, 2),
            pullback_zone=None,
            invalidation_price=round(low, 2),
            stop_loss_price=round(low, 2),
            chase_risk="very_high",
            note="短线结构偏弱，只有重新收复当日高点并放量时才考虑，当前不适合左侧买入",
        )

    if tape.close_position <= 0.25 or tape.change_pct <= -1.0:
        return EntryPlan(
            action="wait",
            setup="weak_reclaim",
            trigger_price=round(high * 1.01, 2),
            pullback_zone=[round(low, 2), round(min(ma5, close), 2)],
            invalidation_price=round(min(low, stop), 2),
            stop_loss_price=round(min(low, stop), 2),
            chase_risk="high",
            note="当日收盘位置偏弱，不用 20 日高点做买点；先等重新收复当日高点并放量，否则只观察",
        )

    neutral_trigger_base = high if tape.close_position < 0.55 else max(high, prev_high20)
    return EntryPlan(
        action="watch",
        setup="neutral_confirmation",
        trigger_price=round(neutral_trigger_base * 1.005, 2),
        pullback_zone=[round(ma10, 2), round(ma5, 2)] if ma10 <= ma5 else [round(ma5, 2), round(ma10, 2)],
        invalidation_price=round(stop, 2),
        stop_loss_price=round(stop, 2),
        chase_risk="medium",
        note="走势未充分确认，等待放量突破触发价，或回踩均线区间缩量企稳",
    )


def estimate_path_distribution(
    df: Optional[pd.DataFrame],
    *,
    trade_date: Optional[str] = None,
    horizons: tuple[int, ...] = (1, 5, 20),
) -> PathDistribution:
    """Estimate empirical forward-return paths from available history."""
    data = _normalize_ohlcv(df)
    if trade_date:
        data = data[data.index <= pd.to_datetime(trade_date)]
    if data.empty or len(data) < 8:
        return PathDistribution(
            windows=[],
            path_shape="insufficient_history",
            path_score=50.0,
            interpretation="path model neutral: insufficient OHLCV history",
        )

    windows: List[PathWindow] = []
    for horizon in horizons:
        samples = _forward_path_samples(data, horizon=horizon)
        if not samples:
            continue
        returns = [item["return_pct"] for item in samples]
        runups = [item["max_runup_pct"] for item in samples]
        drawdowns = [item["max_drawdown_pct"] for item in samples]
        q20 = _quantile(returns, 0.20)
        q80 = _quantile(returns, 0.80)
        windows.append(
            PathWindow(
                horizon_days=horizon,
                sample_size=len(samples),
                mean_return_pct=round(sum(returns) / len(returns), 3),
                median_return_pct=round(_quantile(returns, 0.50), 3),
                q20_return_pct=round(q20, 3),
                q80_return_pct=round(q80, 3),
                win_probability=round(sum(1 for value in returns if value > 0) / len(returns), 3),
                drawdown_probability=round(sum(1 for value in drawdowns if value <= -3.0) / len(drawdowns), 3),
                max_runup_pct=round(_quantile(runups, 0.80), 3),
                max_drawdown_pct=round(_quantile(drawdowns, 0.20), 3),
            )
        )

    if not windows:
        return PathDistribution(
            windows=[],
            path_shape="insufficient_forward_samples",
            path_score=50.0,
            interpretation="path model neutral: no forward samples before latest bar",
        )

    short = _find_path_window(windows, 1) or windows[0]
    swing = _find_path_window(windows, 5) or windows[min(len(windows) - 1, 1)]
    trend = _find_path_window(windows, 20) or windows[-1]
    path_score = _bounded(
        50.0
        + short.mean_return_pct * 2.0
        + swing.mean_return_pct * 2.8
        + trend.mean_return_pct * 1.6
        + (swing.win_probability - 0.50) * 55.0
        + _bounded(trend.q20_return_pct, -12.0, 8.0) * 1.8
        - swing.drawdown_probability * 18.0,
        0.0,
        100.0,
    )
    if swing.q20_return_pct > 0 and trend.win_probability >= 0.58:
        shape = "steady_up"
    elif swing.max_runup_pct >= 6.0 and swing.max_drawdown_pct <= -4.0:
        shape = "volatile_breakout"
    elif trend.q20_return_pct <= -6.0 or swing.drawdown_probability >= 0.45:
        shape = "downside_tail"
    elif abs(swing.mean_return_pct) < 1.0 and 0.43 <= swing.win_probability <= 0.57:
        shape = "range_bound"
    else:
        shape = "mixed"
    interpretation = (
        f"path {shape}: 5d win={swing.win_probability:.0%}, "
        f"5d q20={swing.q20_return_pct:.1f}%, 20d mean={trend.mean_return_pct:.1f}%"
    )
    return PathDistribution(
        windows=windows,
        path_shape=shape,
        path_score=round(path_score, 2),
        interpretation=interpretation,
    )


def build_risk_defense(
    df: Optional[pd.DataFrame],
    *,
    path_distribution: PathDistribution,
    trade_date: Optional[str] = None,
) -> RiskDefense:
    """Quantify volatility shock and downside-tail risk for A-share selection."""
    data = _normalize_ohlcv(df)
    if trade_date:
        data = data[data.index <= pd.to_datetime(trade_date)]
    if data.empty or len(data) < 15:
        return RiskDefense(
            atr_pct=0.0,
            atr_shock_ratio=None,
            downside_tail_pct=None,
            risk_level="unknown",
            risk_penalty=8.0,
            note="risk defense neutral: insufficient ATR history",
        )

    close = float(data["Close"].iloc[-1])
    atr14 = _atr(data, window=14)
    atr_pct = (atr14 / close * 100.0) if close else 0.0
    atr_long = _atr(data.tail(min(len(data), 60)), window=min(50, len(data)))
    atr_shock_ratio = (atr14 / atr_long) if atr_long > 0 else None
    swing = _find_path_window(path_distribution.windows, 5)
    trend = _find_path_window(path_distribution.windows, 20)
    downside_tail = None
    if swing is not None and trend is not None:
        downside_tail = min(swing.q20_return_pct, trend.q20_return_pct)
    elif swing is not None:
        downside_tail = swing.q20_return_pct
    elif trend is not None:
        downside_tail = trend.q20_return_pct

    penalty = 0.0
    penalty += _bounded((atr_pct - 3.0) * 4.0, 0.0, 22.0)
    if atr_shock_ratio is not None:
        penalty += _bounded((atr_shock_ratio - 1.35) * 22.0, 0.0, 24.0)
    if downside_tail is not None:
        penalty += _bounded((-downside_tail - 4.0) * 2.4, 0.0, 28.0)
    penalty = _bounded(penalty, 0.0, 70.0)
    if penalty >= 38:
        level = "high"
    elif penalty >= 18:
        level = "medium"
    else:
        level = "low"
    shock_text = "na" if atr_shock_ratio is None else f"{atr_shock_ratio:.2f}x"
    tail_text = "na" if downside_tail is None else f"{downside_tail:.1f}%"
    return RiskDefense(
        atr_pct=round(atr_pct, 3),
        atr_shock_ratio=None if atr_shock_ratio is None else round(atr_shock_ratio, 3),
        downside_tail_pct=None if downside_tail is None else round(downside_tail, 3),
        risk_level=level,
        risk_penalty=round(penalty, 2),
        note=f"risk {level}: ATR={atr_pct:.1f}%, shock={shock_text}, tail={tail_text}",
    )


def build_walk_forward_calibration(
    df: Optional[pd.DataFrame],
    *,
    tape: TapeAnalysis,
    trend: MultiPeriodTrend,
    trade_date: Optional[str] = None,
    horizon: int = 5,
) -> CalibrationSnapshot:
    """Estimate whether similar historical setups had positive forward returns."""
    data = _normalize_ohlcv(df)
    if trade_date:
        data = data[data.index <= pd.to_datetime(trade_date)]
    min_len = max(35, horizon + 25)
    if data.empty or len(data) < min_len:
        return CalibrationSnapshot(
            sample_size=0,
            hit_rate=None,
            avg_forward_return_pct=None,
            confidence_multiplier=0.72,
            note="calibration low confidence: insufficient walk-forward samples",
        )

    current = _signal_features_from_tape(tape, trend)
    matches: List[float] = []
    last_start = len(data) - horizon - 1
    for idx in range(20, last_start):
        hist_slice = data.iloc[: idx + 1]
        hist_tape = analyze_daily_tape(hist_slice)
        hist_trend = analyze_multi_period_trend(hist_slice)
        if hist_tape is None or hist_trend is None:
            continue
        features = _signal_features_from_tape(hist_tape, hist_trend)
        if _feature_distance(current, features) > 0.38:
            continue
        entry = float(data["Close"].iloc[idx])
        exit_price = float(data["Close"].iloc[idx + horizon])
        if entry > 0:
            matches.append((exit_price / entry - 1.0) * 100.0)
    if not matches:
        return CalibrationSnapshot(
            sample_size=0,
            hit_rate=None,
            avg_forward_return_pct=None,
            confidence_multiplier=0.78,
            note="calibration neutral: no close historical setup matches",
        )
    hit_rate = sum(1 for value in matches if value > 0) / len(matches)
    avg_return = sum(matches) / len(matches)
    size_weight = min(1.0, len(matches) / 20.0)
    edge = (hit_rate - 0.50) * 0.65 + _bounded(avg_return / 8.0, -0.35, 0.35)
    multiplier = _bounded(0.88 + edge * size_weight, 0.62, 1.22)
    return CalibrationSnapshot(
        sample_size=len(matches),
        hit_rate=round(hit_rate, 3),
        avg_forward_return_pct=round(avg_return, 3),
        confidence_multiplier=round(multiplier, 3),
        note=f"calibration 5d: n={len(matches)}, hit={hit_rate:.0%}, avg={avg_return:.1f}%",
    )


def result_to_dict(result: DailySelectionResult) -> Dict[str, Any]:
    return {
        "trade_date": result.trade_date,
        "sectors": [asdict(row) for row in result.sectors],
        "stocks": [asdict(row) for row in result.stocks],
        "reference_pool": [asdict(row) for row in result.reference_pool],
        "action_pool": [asdict(row) for row in result.action_pool],
        "news_impact_summary": result.news_impact_summary,
    }


def _collect_news_rows(items: Iterable[RawNewsItem]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in items:
        meta = item.raw_meta or {}
        market_relevance = _market_relevance_score(item)
        if market_relevance < 0.28:
            continue
        news_impact = _news_impact_score(item)
        if news_impact <= -60 and not meta.get("risk_impacts"):
            continue
        for sector in meta.get("sectors", []) or []:
            sector_name = str(sector.get("sector") or "").strip()
            if not sector_name:
                continue
            rows.append(
                {
                    "sector": sector_name,
                    "title": item.title,
                    "src_name": item.src_name,
                    "news_impact": news_impact,
                    "market_relevance": market_relevance,
                    "leaders": list(sector.get("leaders", []) or []),
                }
            )
    return rows


def _rank_sectors(
    rows: List[Dict[str, Any]],
    *,
    max_sectors: int,
    stocks: Optional[List[StockSelection]] = None,
    flows: Optional[List[Dict[str, Any]]] = None,
) -> List[SectorSelection]:
    stock_by_sector: Dict[str, List[StockSelection]] = {}
    for stock in stocks or []:
        stock_by_sector.setdefault(stock.sector, []).append(stock)
    flow_by_sector = {row["sector"]: row for row in flows or []}

    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        bucket = buckets.setdefault(
            row["sector"],
            {"sector": row["sector"], "count": 0, "impact_sum": 0.0, "risk": 0.0, "relevance_sum": 0.0, "titles": []},
        )
        impact = float(row["news_impact"])
        bucket["count"] += 1
        bucket["impact_sum"] += impact
        bucket["risk"] += max(0.0, -impact)
        bucket["relevance_sum"] += float(row.get("market_relevance") or 0.0)
        _append_unique(bucket["titles"], row["title"], limit=5)
    for flow in flows or []:
        buckets.setdefault(
            flow["sector"],
            {"sector": flow["sector"], "count": 0, "impact_sum": 0.0, "risk": 0.0, "relevance_sum": 0.0, "titles": []},
        )
    out: List[SectorSelection] = []
    for bucket in buckets.values():
        count = bucket["count"]
        avg_impact = bucket["impact_sum"] / count if count else 0.0
        avg_relevance = bucket["relevance_sum"] / count if count else 0.0
        sector_stocks = stock_by_sector.get(bucket["sector"], [])
        confirmed = [stock for stock in sector_stocks if stock.tape_score >= 60]
        tape_confirm = (
            sum(stock.tape_score for stock in sector_stocks[:5]) / min(len(sector_stocks), 5)
            if sector_stocks else 0.0
        )
        flow = flow_by_sector.get(bucket["sector"], {})
        fund_flow_score = _bounded(_safe_float(flow.get("fund_flow_score")), 0.0, 100.0)
        heat = _bounded(
            count * 11.0
            + max(0.0, avg_impact) * 0.55
            + avg_relevance * 18.0
            + tape_confirm * 0.38
            + fund_flow_score * 0.42
            + len(confirmed) * 4.0
            - bucket["risk"] * 0.35,
            0.0,
            100.0,
        )
        out.append(
            SectorSelection(
                sector=bucket["sector"],
                heat_score=round(heat, 2),
                news_count=count,
                avg_news_impact=round(avg_impact, 2),
                risk_penalty=round(bucket["risk"], 2),
                tape_confirm_score=round(tape_confirm, 2),
                confirmed_stock_count=len(confirmed),
                fund_flow_score=round(fund_flow_score, 2),
                main_net_inflow_cny=flow.get("main_net_inflow_cny"),
                fund_flow_rank=flow.get("rank"),
                evidence_titles=list(bucket["titles"]),
            )
        )
    out.sort(key=lambda row: (-row.heat_score, row.sector))
    return out[:max_sectors]


def _normalize_sector_fund_flows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        sector = str(row.get("sector") or row.get("name") or "").strip()
        if not sector:
            continue
        amount = _safe_float(row.get("main_net_inflow_cny", row.get("net_inflow_cny")))
        change_pct = _safe_float(row.get("change_pct"))
        rank = int(_safe_float(row.get("rank"), idx) or idx)
        base = _bounded(72.0 - (rank - 1) * 4.5, 0.0, 72.0)
        amount_score = _bounded(math.log10(abs(amount) + 10.0) * 5.5, 0.0, 22.0) if amount > 0 else 0.0
        change_score = _bounded(change_pct * 2.0, -10.0, 10.0)
        score = _bounded(base + amount_score + change_score, 0.0, 100.0)
        leaders = []
        for leader in row.get("leaders") or []:
            symbol = str(leader.get("symbol") or "").strip()
            if not _is_a_share(symbol):
                continue
            leader_flow = _safe_float(leader.get("main_net_inflow_cny", leader.get("net_inflow_cny")))
            leader_score = _bounded(
                score * 0.7 + (math.log10(abs(leader_flow) + 10.0) * 4.0 if leader_flow > 0 else 0.0),
                0.0,
                100.0,
            )
            leaders.append({**leader, "symbol": symbol, "money_flow_score": round(leader_score, 2)})
        out.append(
            {
                "sector": sector,
                "rank": rank,
                "main_net_inflow_cny": round(amount, 2),
                "change_pct": round(change_pct, 4),
                "fund_flow_score": round(score, 2),
                "leaders": leaders,
            }
        )
    out.sort(key=lambda row: (-_safe_float(row.get("fund_flow_score")), int(row.get("rank") or 999)))
    return out


def _news_impact_score(item: RawNewsItem) -> float:
    meta = item.raw_meta or {}
    relevance = _market_relevance_score(item)
    impact = 0.0
    hot_score = meta.get("hot_score")
    try:
        if hot_score is not None:
            impact += min(18.0, math.log10(float(hot_score) + 10.0) * 4.0)
    except Exception:
        pass
    if meta.get("sectors"):
        impact += 12.0
    for risk in meta.get("risk_impacts", []) or []:
        impact += float(risk.get("csi300_impact_bps", 0.0)) / 4.0
    title = f"{item.title} {item.snippet}"
    positive_words = ("利好", "突破", "增长", "订单", "涨价", "扩产", "政策支持", "国产替代", "创新高")
    negative_words = ("利空", "下滑", "制裁", "调查", "事故", "爆雷", "违约", "冲突", "战争")
    impact += sum(4.0 for word in positive_words if word in title)
    impact -= sum(6.0 for word in negative_words if word in title)
    if relevance < 0.45:
        impact -= (0.45 - relevance) * 70.0
    impact *= _bounded(0.45 + relevance, 0.25, 1.35)
    return _bounded(impact, -100.0, 100.0)


def _market_relevance_score(item: RawNewsItem) -> float:
    """Return 0..1 relevance to A-share trading, not just broad society news."""
    meta = item.raw_meta or {}
    text = f"{item.title} {item.snippet} {item.text}"
    if not text.strip():
        return 0.0

    score = 0.0
    market_terms = (
        "A股", "沪深", "股票", "股价", "涨停", "跌停", "主力资金", "资金净流入",
        "板块", "概念", "ETF", "上市", "港交所", "证监会", "交易所", "财报",
        "业绩", "订单", "扩产", "并购", "融资", "减持", "增持", "回购",
        "政策支持", "国产替代", "突破", "创新高", "半导体", "算力", "机器人",
        "新能源", "白酒", "医药", "军工",
        "stock", "stocks", "share", "shares", "market", "order", "orders",
        "policy", "support", "domestic substitution", "growth", "risk",
        "shock", "semiconductor", "ai", "server", "ev", "battery",
        "$", "SZ", "SH",
    )
    weak_social_terms = (
        "车标", "熊孩子", "饼干", "太好吃", "老人入住", "精神病院", "梅毒",
        "世界杯营销", "好吃", "无妨",
    )
    chatter_terms = (
        "我的股票池", "今天短线", "短线止盈", "卖出：", "趋势追高", "趋势抄底",
        "心态崩", "复盘了一遍", "朋友圈全是", "有人问到", "我觉得",
    )
    for term in market_terms:
        if term in text:
            score += 0.12
    for term in weak_social_terms:
        if term in text:
            score -= 0.22
    for term in chatter_terms:
        if term in text:
            score -= 0.16
    if meta.get("risk_impacts"):
        score += 0.25
    if meta.get("sectors"):
        score += 0.12
    hot_score = meta.get("hot_score")
    if hot_score is not None and score <= 0.12:
        score -= 0.12
    return _bounded(score, 0.0, 1.0)


def _summarize_news_impact(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"count": 0, "positive": 0, "negative": 0, "avg_impact": 0.0}
    impacts = [float(row["news_impact"]) for row in rows]
    return {
        "count": len(rows),
        "positive": sum(1 for value in impacts if value > 0),
        "negative": sum(1 for value in impacts if value < 0),
        "avg_impact": round(sum(impacts) / len(impacts), 2),
    }


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
    out = out.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()
    return out


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    return (
        df.resample(rule)
        .agg(
            {
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }
        )
        .dropna(subset=["Open", "High", "Low", "Close"])
    )


def _trend_frame(df: pd.DataFrame, *, horizon: str, lookback: int, ma_window: int) -> TrendFrame:
    if df.empty or len(df) < 2:
        return TrendFrame(
            horizon=horizon,
            direction="unknown",
            score=50.0,
            change_pct=None,
            ma_gap_pct=None,
            breakout=False,
            breakdown=False,
            interpretation=f"{horizon} 数据不足",
        )

    close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    change_pct = (close / prev_close - 1.0) * 100 if prev_close else 0.0
    ma_window = min(ma_window, len(df))
    ma = float(df["Close"].tail(ma_window).mean()) if ma_window else close
    ma_gap = (close / ma - 1.0) * 100 if ma else 0.0
    previous = df.iloc[:-1]
    previous_window = previous.tail(min(lookback, len(previous)))
    high_prev = float(previous_window["High"].max()) if not previous_window.empty else close
    low_prev = float(previous_window["Low"].min()) if not previous_window.empty else close
    breakout = close > high_prev
    breakdown = close < low_prev

    score = 50.0
    score += _bounded(change_pct * 2.0, -18.0, 18.0)
    score += _bounded(ma_gap * 1.3, -18.0, 18.0)
    if breakout:
        score += 14.0
    if breakdown:
        score -= 18.0
    score = _bounded(score, 0.0, 100.0)

    if score >= 65:
        direction = "up"
    elif score <= 40:
        direction = "down"
    else:
        direction = "sideways"
    labels = {"daily": "日线", "weekly": "周线", "monthly": "月线"}
    label = labels.get(horizon, horizon)
    if breakout:
        interpretation = f"{label}突破前高"
    elif breakdown:
        interpretation = f"{label}跌破区间低点"
    elif direction == "up":
        interpretation = f"{label}均线和涨跌幅偏强"
    elif direction == "down":
        interpretation = f"{label}均线和涨跌幅偏弱"
    else:
        interpretation = f"{label}震荡，方向未确认"
    return TrendFrame(
        horizon=horizon,
        direction=direction,
        score=round(score, 2),
        change_pct=round(change_pct, 3),
        ma_gap_pct=round(ma_gap, 3),
        breakout=breakout,
        breakdown=breakdown,
        interpretation=interpretation,
    )


def _atr(df: pd.DataFrame, *, window: int = 14) -> float:
    if df.empty:
        return 0.0
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    value = tr.tail(window).mean()
    try:
        return float(value)
    except Exception:
        return 0.0


def _forward_path_samples(df: pd.DataFrame, *, horizon: int) -> List[Dict[str, float]]:
    if horizon <= 0 or len(df) <= horizon + 1:
        return []
    out: List[Dict[str, float]] = []
    closes = df["Close"].astype(float).reset_index(drop=True)
    highs = df["High"].astype(float).reset_index(drop=True)
    lows = df["Low"].astype(float).reset_index(drop=True)
    # Exclude the latest bar as a sample origin because its future is unknown.
    for idx in range(0, len(closes) - horizon - 1):
        entry = float(closes.iloc[idx])
        if entry <= 0:
            continue
        end = idx + horizon
        exit_price = float(closes.iloc[end])
        window_high = float(highs.iloc[idx + 1 : end + 1].max())
        window_low = float(lows.iloc[idx + 1 : end + 1].min())
        out.append(
            {
                "return_pct": (exit_price / entry - 1.0) * 100.0,
                "max_runup_pct": (window_high / entry - 1.0) * 100.0,
                "max_drawdown_pct": (window_low / entry - 1.0) * 100.0,
            }
        )
    return out


def _find_path_window(windows: List[PathWindow], horizon_days: int) -> Optional[PathWindow]:
    for window in windows:
        if window.horizon_days == horizon_days:
            return window
    return None


def _quantile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    q = _bounded(q, 0.0, 1.0)
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    weight = pos - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def _signal_features_from_tape(tape: TapeAnalysis, trend: MultiPeriodTrend) -> Dict[str, float]:
    return {
        "change": _bounded(tape.change_pct / 10.0, -1.0, 1.0),
        "close_position": _bounded(tape.close_position, 0.0, 1.0),
        "volume_ratio": _bounded(((tape.volume_ratio or 1.0) - 1.0) / 3.0, -1.0, 1.0),
        "tape_score": _bounded((tape.tape_score - 50.0) / 50.0, -1.0, 1.0),
        "trend_score": _bounded((trend.alignment_score - 50.0) / 50.0, -1.0, 1.0),
        "breakout": 1.0 if tape.breakout_20d else 0.0,
        "breakdown": 1.0 if tape.breakdown_20d else 0.0,
    }


def _feature_distance(left: Dict[str, float], right: Dict[str, float]) -> float:
    keys = sorted(set(left) | set(right))
    if not keys:
        return 1.0
    total = 0.0
    for key in keys:
        total += abs(float(left.get(key, 0.0)) - float(right.get(key, 0.0)))
    return total / len(keys)


def _interpret_tape(
    *,
    change_pct: float,
    close_position: float,
    volume_ratio: Optional[float],
    breakout: bool,
    breakdown: bool,
    limit_up_like: bool,
    ma5_slope: Optional[float],
) -> str:
    if limit_up_like:
        return "涨停或接近涨停收盘，日内资金一致性强，但次日追高风险也高"
    if breakout and volume_ratio and volume_ratio >= 1.4:
        return "放量突破近 20 日压力，走势确认度较高"
    if breakout:
        return "突破近 20 日压力，但量能确认一般"
    if breakdown:
        return "跌破近 20 日支撑，短线结构转弱"
    if change_pct > 2.0 and close_position >= 0.7:
        return "上涨并靠近日高收盘，日内承接较强"
    if change_pct > 0 and close_position < 0.35:
        return "收涨但明显回落，可能存在冲高兑现"
    if change_pct < -2.0 and close_position <= 0.35:
        return "下跌并靠近日低收盘，短线抛压仍重"
    if ma5_slope is not None and ma5_slope > 0.8:
        return "短期均线抬升，趋势有改善"
    return "走势中性，需要等待量价进一步确认"


def _attention_label(score: float, tape: TapeAnalysis, risk_penalty: float) -> str:
    if risk_penalty >= 35:
        return "risk_watch"
    if score >= 78 and tape.tape_score >= 65:
        return "priority_watch"
    if score >= 60:
        return "watch"
    return "observe"


def _is_a_share(symbol: str) -> bool:
    return symbol.isdigit() and len(symbol) == 6 and symbol[0] in {"0", "2", "3", "6", "8", "4"}


def _append_unique(target: List[str], value: str, *, limit: int) -> None:
    value = (value or "").strip()
    if value and value not in target and len(target) < limit:
        target.append(value)


def _bounded(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            if not text or text in {"-", "--"}:
                return default
            multiplier = 1.0
            if text.endswith("亿"):
                multiplier = 100_000_000.0
                text = text[:-1]
            elif text.endswith("万"):
                multiplier = 10_000.0
                text = text[:-1]
            if text.endswith("%"):
                text = text[:-1]
            return float(text) * multiplier
        return float(value)
    except (TypeError, ValueError):
        return default


def _infer_latest_date(history_by_symbol: Dict[str, pd.DataFrame]) -> str:
    latest = None
    for df in history_by_symbol.values():
        data = _normalize_ohlcv(df)
        if data.empty:
            continue
        idx = data.index[-1]
        latest = idx if latest is None or idx > latest else latest
    return _date_str(latest) if latest is not None else ""


def _date_str(value: Any) -> str:
    return pd.to_datetime(value).strftime("%Y-%m-%d")


__all__ = [
    "DailySelectionResult",
    "Affordability",
    "CalibrationSnapshot",
    "EntryPlan",
    "MultiPeriodTrend",
    "PathDistribution",
    "PathWindow",
    "RiskDefense",
    "SectorSelection",
    "StockSelection",
    "TapeAnalysis",
    "TrendFrame",
    "analyze_daily_tape",
    "analyze_multi_period_trend",
    "build_affordability",
    "build_daily_selection",
    "build_entry_plan",
    "build_risk_defense",
    "build_walk_forward_calibration",
    "estimate_path_distribution",
    "result_to_dict",
]
