from __future__ import annotations

import pandas as pd

from core.daily_stock_selector import (
    analyze_daily_tape,
    analyze_multi_period_trend,
    build_affordability,
    build_daily_selection,
    build_entry_plan,
    build_risk_defense,
    build_walk_forward_calibration,
    estimate_path_distribution,
    result_to_dict,
)
from services.news_sources import RawNewsItem


def _trend_df(*, breakout: bool = True) -> pd.DataFrame:
    rows = []
    close = 10.0
    for i in range(25):
        open_price = close
        close = close + 0.12
        high = close + 0.08
        low = open_price - 0.06
        volume = 1000 + i * 20
        rows.append((open_price, high, low, close, volume))
    if breakout:
        rows[-1] = (12.8, 14.0, 12.7, 13.9, 3200)
    idx = pd.date_range("2026-06-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def _weak_df() -> pd.DataFrame:
    rows = []
    close = 20.0
    for i in range(25):
        open_price = close
        close = close - 0.12
        high = open_price + 0.04
        low = close - 0.08
        rows.append((open_price, high, low, close, 1000 + i * 10))
    idx = pd.date_range("2026-06-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def _long_trend_df() -> pd.DataFrame:
    rows = []
    close = 10.0
    for i in range(90):
        open_price = close
        drift = 0.08 + (0.02 if i % 7 in {0, 1, 2} else -0.01)
        close = close + drift
        high = close + 0.10
        low = open_price - 0.05
        volume = 1200 + i * 12
        rows.append((open_price, high, low, close, volume))
    rows[-1] = (close - 0.1, close + 0.55, close - 0.22, close + 0.48, 3600)
    idx = pd.date_range("2026-03-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def test_analyze_daily_tape_detects_breakout_and_volume_confirmation():
    tape = analyze_daily_tape(_trend_df())

    assert tape is not None
    assert tape.breakout_20d is True
    assert tape.volume_ratio and tape.volume_ratio > 2
    assert tape.tape_score > 75
    assert "突破" in tape.interpretation


def test_analyze_daily_tape_accepts_cached_history_without_open():
    tape = analyze_daily_tape(_trend_df().drop(columns=["Open"]))

    assert tape is not None
    assert tape.close > 0
    assert tape.tape_score > 60


def test_analyze_multi_period_trend_returns_daily_weekly_monthly_frames():
    trend = analyze_multi_period_trend(_trend_df())

    assert trend is not None
    assert trend.daily.horizon == "daily"
    assert trend.weekly.horizon == "weekly"
    assert trend.monthly.horizon == "monthly"
    assert trend.alignment in {"bullish_alignment", "partial_bullish", "mixed", "bearish_pressure"}


def test_path_distribution_risk_defense_and_calibration_are_numeric():
    df = _long_trend_df()
    tape = analyze_daily_tape(df)
    trend = analyze_multi_period_trend(df)

    path = estimate_path_distribution(df)
    risk = build_risk_defense(df, path_distribution=path)
    calibration = build_walk_forward_calibration(df, tape=tape, trend=trend)

    assert {window.horizon_days for window in path.windows} == {1, 5, 20}
    assert 0 <= path.path_score <= 100
    assert path.path_shape in {"steady_up", "volatile_breakout", "downside_tail", "range_bound", "mixed"}
    assert risk.risk_level in {"low", "medium", "high"}
    assert risk.risk_penalty >= 0
    assert 0.62 <= calibration.confidence_multiplier <= 1.22


def test_affordability_blocks_stock_when_cash_cannot_buy_one_lot():
    result = build_affordability(1291.91, available_cash_cny=50_000)

    assert result.affordable is False
    assert result.min_lot_cash == 129191
    assert result.max_lots == 0


def test_entry_plan_waits_for_pullback_after_extended_strength():
    df = _trend_df()
    tape = analyze_daily_tape(df)

    plan = build_entry_plan(df, tape=tape)

    assert plan.action == "wait_pullback"
    assert plan.pullback_zone is not None
    assert plan.chase_risk == "high"


def test_entry_plan_waits_after_fade():
    df = _trend_df()
    df.iloc[-1, df.columns.get_loc("Open")] = 13.8
    df.iloc[-1, df.columns.get_loc("High")] = 14.2
    df.iloc[-1, df.columns.get_loc("Low")] = 13.4
    df.iloc[-1, df.columns.get_loc("Close")] = 13.45
    tape = analyze_daily_tape(df)

    plan = build_entry_plan(df, tape=tape)

    assert plan.action == "wait"
    assert plan.setup == "fade_after_rally"
    assert plan.trigger_price and plan.trigger_price > tape.close


def test_entry_plan_uses_reclaim_trigger_for_weak_close_without_breakdown():
    df = _trend_df(breakout=False)
    df.iloc[-1, df.columns.get_loc("Open")] = 12.9
    df.iloc[-1, df.columns.get_loc("High")] = 13.1
    df.iloc[-1, df.columns.get_loc("Low")] = 12.55
    df.iloc[-1, df.columns.get_loc("Close")] = 12.66
    tape = analyze_daily_tape(df)

    plan = build_entry_plan(df, tape=tape)

    assert plan.action == "wait"
    assert plan.setup == "weak_reclaim"
    assert plan.trigger_price == 13.23


def test_daily_selection_ranks_hot_a_share_leaders_only():
    items = [
        RawNewsItem(
            src_name="baidu_hot",
            title="AI server orders accelerate",
            url="https://n.example/1",
            snippet="policy support and domestic substitution",
            raw_meta={
                "hot_score": 900000,
                "sectors": [
                    {
                        "sector": "AI infrastructure",
                        "leaders": [
                            {"symbol": "601138", "name": "Foxconn Industrial", "reason": "AI server leader"},
                            {"symbol": "00700", "name": "Tencent", "reason": "HK platform"},
                        ],
                    }
                ],
            },
        ),
        RawNewsItem(
            src_name="cls",
            title="Credit risk shock hits growth stocks",
            url="https://n.example/2",
            snippet="risk-off pressure",
            raw_meta={
                "sectors": [
                    {
                        "sector": "AI infrastructure",
                        "leaders": [{"symbol": "300476", "name": "Shenghong Tech", "reason": "PCB leader"}],
                    }
                ],
                "risk_impacts": [{"csi300_impact_bps": -80.0}],
            },
        ),
    ]
    result = build_daily_selection(
        items,
        {
            "601138": _trend_df(),
            "300476": _weak_df(),
            "00700": _trend_df(),
        },
        max_stocks=10,
    )

    assert result.sectors[0].sector == "AI infrastructure"
    assert result.sectors[0].news_count == 2
    symbols = [row.symbol for row in result.stocks]
    assert "601138" in symbols
    assert "300476" in symbols
    assert "00700" not in symbols
    assert result.stocks[0].symbol == "601138"
    assert result.stocks[0].attention in {"priority_watch", "watch"}
    assert result.news_impact_summary["negative"] == 1
    assert result.stocks[0].trend.daily.horizon == "daily"
    assert result.stocks[0].affordability.affordable is None
    assert result.stocks[0].path_distribution.windows
    assert result.stocks[0].risk_defense.risk_level in {"low", "medium", "high"}
    assert result.stocks[0].model_edge_score >= 0
    payload = result_to_dict(result)
    assert "path_distribution" in payload["stocks"][0]
    assert "calibration" in payload["stocks"][0]


def test_daily_selection_filters_unaffordable_a_share_lots():
    items = [
        RawNewsItem(
            src_name="cls",
            title="White liquor leader attracts market attention",
            url="https://n.example/3",
            snippet="A share sector leader",
            raw_meta={
                "sectors": [
                    {
                        "sector": "consumption",
                        "leaders": [{"symbol": "600519", "name": "Kweichow Moutai", "reason": "expensive leader"}],
                    }
                ],
            },
        )
    ]
    expensive = _trend_df()
    expensive["Open"] = expensive["Open"] * 100
    expensive["High"] = expensive["High"] * 100
    expensive["Low"] = expensive["Low"] * 100
    expensive["Close"] = expensive["Close"] * 100

    result = build_daily_selection(
        items,
        {"600519": expensive},
        available_cash_cny=50_000,
        max_stocks=10,
    )

    assert result.stocks == []
    assert result.news_impact_summary["affordability_filtered"] == 1


def test_daily_selection_uses_sector_fund_flow_and_fundamentals():
    result = build_daily_selection(
        [],
        {"603308": _trend_df()},
        sector_fund_flows=[
            {
                "sector": "商业航天",
                "rank": 1,
                "main_net_inflow_cny": 520_000_000,
                "change_pct": 2.8,
                "leaders": [
                    {
                        "symbol": "603308",
                        "name": "应流股份",
                        "main_net_inflow_cny": 80_000_000,
                        "reason": "板块内主力资金靠前",
                    }
                ],
            }
        ],
        fundamentals_by_symbol={
            "603308": {"score": 82, "model": "industrial_quality_value", "reason": "基本面质量较好"}
        },
        max_stocks=5,
    )

    assert result.sectors[0].sector == "商业航天"
    assert result.sectors[0].fund_flow_score > 70
    assert result.stocks[0].symbol == "603308"
    assert result.stocks[0].money_flow_score > 70
    assert result.stocks[0].fundamental_score == 82
    assert "基本面质量较好" in result.stocks[0].reasons
