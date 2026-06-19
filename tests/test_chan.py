"""缠论与技术形态检测逻辑单元测试
"""
from __future__ import annotations

import pandas as pd
import pytest

from utils.chan import (
    remove_inclusions,
    find_fractals,
    build_bi,
    build_zhongshu,
    detect_double_patterns,
    detect_failed_limit_up,
    analyze_chan,
    format_chan_brief,
    detect_head_shoulders,
    detect_triple_patterns,
    detect_rsi_divergence,
    detect_gaps,
)


def test_remove_inclusions():
    # 构造带包含关系的 K 线数据
    # Bar 0: High=10, Low=5
    # Bar 1: High=9, Low=6 (被 Bar 0 包含)
    # Bar 2: High=12, Low=7 (未包含，上升)
    dates = pd.date_range("2026-06-01", periods=3)
    df = pd.DataFrame({
        "High": [10.0, 9.0, 12.0],
        "Low": [5.0, 6.0, 7.0],
        "Close": [8.0, 7.5, 11.0],
    }, index=dates)

    merged = remove_inclusions(df)
    assert len(merged) == 2
    # 合并后第一根 K 线 (由于初始方向为 up，合并高点取 max(10,9)=10, 低点取 max(5,6)=6)
    assert merged[0]['high'] == 10.0
    assert merged[0]['low'] == 6.0
    # 合并后第二根 K 线对应原始第三根 K 线
    assert merged[1]['high'] == 12.0
    assert merged[1]['low'] == 7.0


def test_find_fractals():
    # 构造顶分型：中间 K 线的 High 最高且 Low 最高
    # 构造底分型：中间 K 线的 Low 最低且 High 最低
    merged = [
        {'date': '2026-06-01', 'high': 10.0, 'low': 5.0, 'close': 8.0},
        {'date': '2026-06-02', 'high': 12.0, 'low': 7.0, 'close': 11.0}, # 顶分型候选
        {'date': '2026-06-03', 'high': 9.0, 'low': 6.0, 'close': 8.0},
        {'date': '2026-06-04', 'high': 6.0, 'low': 3.0, 'close': 4.0},  # 底分型候选
        {'date': '2026-06-05', 'high': 8.0, 'low': 5.0, 'close': 7.0},
    ]
    fractals = find_fractals(merged)
    assert len(fractals) == 2
    assert fractals[0]['type'] == 'top'
    assert fractals[0]['idx'] == 1
    assert fractals[1]['type'] == 'bottom'
    assert fractals[1]['idx'] == 3


def test_build_bi():
    # 构造笔数据：顶分型与底分型交替，且索引差 >= 4
    # Merged 长度 10
    merged = [
        {'date': 'D0', 'high': 10.0, 'low': 5.0},
        {'date': 'D1', 'high': 15.0, 'low': 10.0}, # Top Fractal at index 1
        {'date': 'D2', 'high': 12.0, 'low': 8.0},
        {'date': 'D3', 'high': 10.0, 'low': 6.0},
        {'date': 'D4', 'high': 8.0, 'low': 4.0},
        {'date': 'D5', 'high': 6.0, 'low': 2.0},   # Bottom Fractal at index 5
        {'date': 'D6', 'high': 8.0, 'low': 4.0},
        {'date': 'D7', 'high': 10.0, 'low': 6.0},
        {'date': 'D8', 'high': 12.0, 'low': 8.0},
        {'date': 'D9', 'high': 14.0, 'low': 9.0},  # Top Fractal at index 9 (or end)
    ]

    # 注入 fractals 模拟
    fractals = [
        {'type': 'top', 'idx': 1, 'high': 15.0, 'low': 10.0, 'date': 'D1'},
        {'type': 'bottom', 'idx': 5, 'high': 6.0, 'low': 2.0, 'date': 'D5'},
        {'type': 'top', 'idx': 9, 'high': 14.0, 'low': 9.0, 'date': 'D9'},
    ]

    bis = build_bi(merged, fractals)
    assert len(bis) == 2
    assert bis[0]['type'] == 'down'
    assert bis[0]['start_idx'] == 1
    assert bis[0]['end_idx'] == 5
    assert bis[0]['start_val'] == 15.0
    assert bis[0]['end_val'] == 2.0

    assert bis[1]['type'] == 'up'
    assert bis[1]['start_idx'] == 5
    assert bis[1]['end_idx'] == 9
    assert bis[1]['start_val'] == 2.0
    assert bis[1]['end_val'] == 14.0


def test_build_zhongshu():
    # 构造 3 笔相互重叠的数据
    # Bi 0 (down): 10 -> 2
    # Bi 1 (up):   2 -> 8
    # Bi 2 (down): 8 -> 4
    # 重叠区间应该在 [max(2, 2, 4), min(10, 8, 8)] = [4, 8]
    bis = [
        {'type': 'down', 'start_val': 10.0, 'end_val': 2.0, 'start_date': 'D0', 'end_date': 'D1'},
        {'type': 'up', 'start_val': 2.0, 'end_val': 8.0, 'start_date': 'D1', 'end_date': 'D2'},
        {'type': 'down', 'start_val': 8.0, 'end_val': 4.0, 'start_date': 'D2', 'end_date': 'D3'},
    ]
    zhongshus = build_zhongshu(bis)
    assert len(zhongshus) == 1
    assert zhongshus[0]['zd'] == 4.0
    assert zhongshus[0]['zg'] == 8.0


def test_detect_double_patterns():
    # 构造 M顶数据：
    # 价格上涨到 100, 回调到 90, 再上涨到 99, 然后下跌到 88 (跌破 90 颈线)
    prices = [50] * 10 + [100] + [95] + [90] + [95] + [99] + [93] + [88]
    df = pd.DataFrame({"Close": prices}, index=pd.date_range("2026-05-01", periods=len(prices)))

    results = detect_double_patterns(df)
    assert results['m_top'] is True
    assert results['m_top_peaks'] == (100.0, 99.0)
    assert results['m_top_neckline'] == 90.0

    # 构造 W底数据：
    # 价格下跌到 50, 反弹到 60, 再下跌到 51, 然后上涨到 63 (突破 60 颈线)
    prices_w = [80] * 10 + [50] + [55] + [60] + [55] + [51] + [58] + [63]
    df_w = pd.DataFrame({"Close": prices_w}, index=pd.date_range("2026-05-01", periods=len(prices_w)))

    results_w = detect_double_patterns(df_w)
    assert results_w['w_bottom'] is True
    assert results_w['w_bottom_valleys'] == (50.0, 51.0)
    assert results_w['w_bottom_neckline'] == 60.0


def test_detect_failed_limit_up():
    # A股 10% 涨停板规则下，今日前一日 Close=10.0，理论涨停价=11.0
    # 今日 High=11.0，但今日 Close=10.50 (涨幅 5.0%，封板失败)
    dates = pd.date_range("2026-06-01", periods=5)
    df = pd.DataFrame({
        "Close": [10.0, 10.0, 10.0, 10.0, 10.5],
        "High":  [10.0, 10.0, 10.0, 10.0, 11.0],
        "Low":   [10.0, 10.0, 10.0, 10.0, 10.0],
    }, index=dates)

    events = detect_failed_limit_up(df, "600900")
    assert len(events) == 1
    assert events[0]['limit_price'] == 11.0
    assert events[0]['high'] == 11.0
    assert events[0]['close'] == 10.5
    assert events[0]['change_pct'] == 5.0


def test_analyze_and_format():
    # 简单调用 analyze_chan 和 format_chan_brief 并进行烟雾测试
    dates = pd.date_range("2026-06-01", periods=5)
    df = pd.DataFrame({
        "Close": [10.0, 10.0, 10.0, 10.0, 10.5],
        "High":  [10.0, 10.0, 10.0, 10.0, 11.0],
        "Low":   [10.0, 10.0, 10.0, 10.0, 10.0],
    }, index=dates)

    chan_data = analyze_chan(df, "600000", "测试标的")
    brief = format_chan_brief(chan_data)
    assert "封板失败" in brief
    assert "【M顶】" not in brief


def test_detect_head_shoulders():
    # 构造头肩顶: LS=100, head=120, RS=99, Valley1=80, Valley2=81, current=75 (跌破颈线)
    closes = [50] * 30 + [100] + [80] + [120] + [81] + [99] + [75]
    df = pd.DataFrame({"Close": closes}, index=pd.date_range("2026-05-01", periods=len(closes)))

    res = detect_head_shoulders(df)
    assert res['head_shoulders'] is True
    assert res['head_shoulders_type'] == 'top'
    assert res['head_shoulders_values'] == (100.0, 120.0, 99.0)
    assert res['head_shoulders_neckline'] == 81.0

    # 构造头肩底: LS=100, head=80, RS=101, Peak1=120, Peak2=119, current=125 (突破颈线)
    closes_bottom = [150] * 30 + [100] + [120] + [80] + [119] + [101] + [125]
    df_bottom = pd.DataFrame({"Close": closes_bottom}, index=pd.date_range("2026-05-01", periods=len(closes_bottom)))

    res_b = detect_head_shoulders(df_bottom)
    assert res_b['head_shoulders'] is True
    assert res_b['head_shoulders_type'] == 'bottom'
    assert res_b['head_shoulders_values'] == (100.0, 80.0, 101.0)
    assert res_b['head_shoulders_neckline'] == 119.0


def test_detect_triple_patterns():
    # 构造三重顶: Peaks=100, 101, 99, Valley1=80, Valley2=79, current=75
    closes = [50] * 25 + [100] + [80] + [101] + [79] + [99] + [75]
    df = pd.DataFrame({"Close": closes}, index=pd.date_range("2026-05-01", periods=len(closes)))

    res = detect_triple_patterns(df)
    assert res['triple_top'] is True
    assert res['triple_top_peaks'] == (100.0, 101.0, 99.0)
    assert res['triple_top_neckline'] == 79.0


def test_detect_rsi_divergence():
    # 构造顶背离: 价格创新高(15.0 -> 15.5)，但由于第二波上涨幅度变小，导致 RSI 峰值降低
    closes = [10.0] * 35 + [15.0] + [12.0] * 10 + [15.5] + [12.0]
    df = pd.DataFrame({"Close": closes}, index=pd.date_range("2026-05-01", periods=len(closes)))

    res = detect_rsi_divergence(df)
    assert res['bearish_divergence'] is True


def test_detect_gaps():
    # 构造跳空缺口
    # Day 0 to 9: Close/High/Low = 10.0
    # Day 10: Low = 12.0, High = 13.0, Close = 12.5 (向上跳空缺口 10.0 -> 12.0)
    dates = pd.date_range("2026-06-01", periods=11)
    df = pd.DataFrame({
        "High":  [10.0] * 10 + [13.0],
        "Low":   [10.0] * 10 + [12.0],
        "Close": [10.0] * 10 + [12.5],
    }, index=dates)

    gaps = detect_gaps(df)
    assert len(gaps) == 1
    assert gaps[0]['type'] == 'up'
    assert gaps[0]['gap_range'] == (10.0, 12.0)
    assert gaps[0]['filled'] is False
