"""缠论与技术形态分析组件

包括：
- K线包含关系合并 (remove_inclusions)
- 顶底分型识别 (find_fractals)
- 笔构建 (build_bi)
- 中枢识别 (build_zhongshu)
- 双顶(M顶)/双底(W底)形态检测 (detect_double_patterns)
- 头肩顶/头肩底反转形态检测 (detect_head_shoulders)
- 三重顶/三重底形态检测 (detect_triple_patterns)
- RSI指标与价格顶底背离检测 (detect_rsi_divergence)
- 日线跳空缺口及回补检测 (detect_gaps)
- A股封板失败/冲高回落检测 (detect_failed_limit_up)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
import pandas as pd
import json
import os

PARAMS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "chan_patterns_params.json")

def load_params() -> dict:
    if os.path.exists(PARAMS_PATH):
        try:
            with open(PARAMS_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _get_param(symbol: str, param_name: str) -> Any:
    defaults = {
        "gap_threshold_pct": 0.003,
        "rsi_overbought": 65.0,
        "rsi_oversold": 35.0,
        "failed_limit_up_vol_mult": 1.2,
        "failed_limit_up_shadow_pct": 0.02,
        "trend_window": 60
    }
    params = load_params()
    symbol_str = str(symbol).strip()
    if symbol_str in params and param_name in params[symbol_str]:
        return params[symbol_str][param_name]
    if "default" in params and param_name in params["default"]:
        return params["default"][param_name]
    return defaults[param_name]



def remove_inclusions(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    K线包含关系处理 (包含合并)。
    要求 df 包含 'High' 和 'Low' 列。
    返回无包含关系的 K 线序列列表。
    """
    if df.empty or "High" not in df.columns or "Low" not in df.columns:
        return []

    raw_klines = []
    for idx, row in df.iterrows():
        raw_klines.append({
            'date': idx,
            'high': float(row['High']),
            'low': float(row['Low']),
            'close': float(row['Close']),
            'volume': float(row['Volume']) if 'Volume' in row else 0.0
        })

    if len(raw_klines) <= 1:
        return raw_klines

    merged = [raw_klines[0]]
    direction = 'up'  # 初始方向假设为向上

    for i in range(1, len(raw_klines)):
        curr = raw_klines[i]
        prev = merged[-1]

        # 判断是否存在包含关系
        is_prev_includes_curr = (prev['high'] >= curr['high'] and prev['low'] <= curr['low'])
        is_curr_includes_prev = (curr['high'] >= prev['high'] and curr['low'] <= prev['low'])

        if is_prev_includes_curr or is_curr_includes_prev:
            # 确定当前方向
            if len(merged) > 1:
                pre_prev = merged[-2]
                if prev['high'] > pre_prev['high']:
                    direction = 'up'
                elif prev['high'] < pre_prev['high']:
                    direction = 'down'

            # 根据方向执行合并
            if direction == 'up':
                new_high = max(prev['high'], curr['high'])
                new_low = max(prev['low'], curr['low'])
            else:
                new_high = min(prev['high'], curr['high'])
                new_low = min(prev['low'], curr['low'])

            # 在原 K 线上更新合并后的值
            prev['high'] = new_high
            prev['low'] = new_low
            # 保留最新的日期、收盘价和成交量累加
            prev['close'] = curr['close']
            prev['date'] = curr['date']
            prev['volume'] = prev['volume'] + curr['volume']
        else:
            merged.append(curr)

    return merged


def find_fractals(merged: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    寻找合并包含关系后的顶底分型。
    """
    fractals = []
    if len(merged) < 3:
        return fractals

    for i in range(1, len(merged) - 1):
        prev = merged[i-1]
        curr = merged[i]
        nxt = merged[i+1]

        is_top = (curr['high'] > prev['high'] and curr['high'] > nxt['high'] and
                  curr['low'] > prev['low'] and curr['low'] > nxt['low'])
        is_bottom = (curr['low'] < prev['low'] and curr['low'] < nxt['low'] and
                     curr['high'] < prev['high'] and curr['high'] < nxt['high'])

        if is_top:
            fractals.append({
                'type': 'top',
                'idx': i,
                'high': curr['high'],
                'low': curr['low'],
                'date': curr['date']
            })
        elif is_bottom:
            fractals.append({
                'type': 'bottom',
                'idx': i,
                'high': curr['high'],
                'low': curr['low'],
                'date': curr['date']
            })
    return fractals


def build_bi(merged: List[Dict[str, Any]], fractals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    构建符合缠论标准的笔。
    要求：顶底交替，且两个相邻分型之间(即端点索引差)至少相隔 4 个 K 线 (即总计 5 根 K 线)。
    """
    bis = []
    if len(fractals) < 2:
        return bis

    i = 0
    while i < len(fractals):
        curr_f = fractals[i]
        best_opp = None
        best_opp_idx = -1

        j = i + 1
        while j < len(fractals):
            nxt_f = fractals[j]
            if nxt_f['type'] == curr_f['type']:
                # 同类型分型：若比当前更极端，则更新起点，重置主循环起点
                if curr_f['type'] == 'top' and nxt_f['high'] > curr_f['high']:
                    curr_f = nxt_f
                    i = j
                elif curr_f['type'] == 'bottom' and nxt_f['low'] < curr_f['low']:
                    curr_f = nxt_f
                    i = j
            else:
                # 相反类型分型：检验距离限制 (索引差 >= 4) 和价格是否突破/合理
                idx_diff = nxt_f['idx'] - curr_f['idx']
                price_ok = False
                if curr_f['type'] == 'top':
                    price_ok = nxt_f['low'] < curr_f['high']
                else:
                    price_ok = nxt_f['high'] > curr_f['low']

                if idx_diff >= 4 and price_ok:
                    # 这是一个候选的相反方向分型点
                    # 寻找最极端的候选点 (寻找更低的底，或更高的顶)
                    if best_opp is None:
                        best_opp = nxt_f
                        best_opp_idx = j
                    else:
                        if curr_f['type'] == 'top':
                            if nxt_f['low'] < best_opp['low']:
                                best_opp = nxt_f
                                best_opp_idx = j
                        else:
                            if nxt_f['high'] > best_opp['high']:
                                best_opp = nxt_f
                                best_opp_idx = j
            j += 1

        if best_opp is not None:
            bis.append({
                'type': 'down' if curr_f['type'] == 'top' else 'up',
                'start_idx': curr_f['idx'],
                'end_idx': best_opp['idx'],
                'start_val': curr_f['high'] if curr_f['type'] == 'top' else curr_f['low'],
                'end_val': best_opp['low'] if best_opp['type'] == 'bottom' else best_opp['high'],
                'start_date': curr_f['date'],
                'end_date': best_opp['date']
            })
            # 确认该笔，下一个起点从被确认的分型开始
            i = best_opp_idx
        else:
            # 找不到符合条件的下一个分型，终止
            break

    return bis


def build_zhongshu(bis: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    构建缠论价格中枢。
    定义：由至少 3 笔有价格重叠的连续笔构成，其中枢区间为前三笔重叠的价格投影范围 [ZD, ZG]。
    """
    zhongshus = []
    if len(bis) < 3:
        return zhongshus

    i = 0
    while i < len(bis) - 2:
        b1, b2, b3 = bis[i], bis[i+1], bis[i+2]

        l1, h1 = min(b1['start_val'], b1['end_val']), max(b1['start_val'], b1['end_val'])
        l2, h2 = min(b2['start_val'], b2['end_val']), max(b2['start_val'], b2['end_val'])
        l3, h3 = min(b3['start_val'], b3['end_val']), max(b3['start_val'], b3['end_val'])

        # 前三笔的价格重叠区间
        zd = max(l1, l2, l3)
        zg = min(h1, h2, h3)

        if zd < zg:
            # 重叠成立，构成中枢
            start_idx = i
            end_idx = i + 2

            # 扩展该中枢：检测后续的笔是否和中枢区间有重叠
            j = i + 3
            while j < len(bis):
                bj = bis[j]
                lj, hj = min(bj['start_val'], bj['end_val']), max(bj['start_val'], bj['end_val'])
                if max(zd, lj) < min(zg, hj):
                    end_idx = j
                    j += 1
                else:
                    break

            zhongshus.append({
                'zd': zd,
                'zg': zg,
                'start_bi_idx': start_idx,
                'end_bi_idx': end_idx,
                'start_date': bis[start_idx]['start_date'],
                'end_date': bis[end_idx]['end_date']
            })
            # 继续寻找下一个中枢
            i = end_idx + 1
        else:
            i += 1

    return zhongshus


def detect_double_patterns(df: pd.DataFrame) -> Dict[str, Any]:
    """
    根据最近 60 个交易日的收盘价，识别 M顶 (双顶) 或 W底 (双底) 结构。
    """
    closes = df['Close'].tolist()
    dates = df.index.tolist()
    n = len(closes)

    results = {
        'm_top': False,
        'm_top_date': None,
        'm_top_peaks': None,
        'm_top_neckline': None,
        'w_bottom': False,
        'w_bottom_date': None,
        'w_bottom_valleys': None,
        'w_bottom_neckline': None
    }

    if n < 15:
        return results

    window = min(60, n)
    recent_closes = closes[-window:]
    recent_dates = dates[-window:]

    # 寻找局部极值
    extrema = []
    for i in range(1, window - 1):
        if recent_closes[i] > recent_closes[i-1] and recent_closes[i] > recent_closes[i+1]:
            extrema.append({
                'type': 'peak',
                'val': recent_closes[i],
                'idx': i,
                'date': recent_dates[i]
            })
        elif recent_closes[i] < recent_closes[i-1] and recent_closes[i] < recent_closes[i+1]:
            extrema.append({
                'type': 'valley',
                'val': recent_closes[i],
                'idx': i,
                'date': recent_dates[i]
            })

    current_price = closes[-1]

    # 检测 M顶: Peak1 -> Valley -> Peak2 模式，且当前价格已跌破或极为接近颈线 (Valley)
    for i in range(len(extrema) - 2):
        e1, e2, e3 = extrema[i], extrema[i+1], extrema[i+2]
        if e1['type'] == 'peak' and e2['type'] == 'valley' and e3['type'] == 'peak':
            p1, v, p2 = e1['val'], e2['val'], e3['val']

            # 两个顶相差不超过 4%
            peaks_close = abs(p1 - p2) / max(p1, p2) <= 0.04
            # 颈线深度大于 3% (即高点回调幅度)
            valley_deep = (min(p1, p2) - v) / min(p1, p2) >= 0.03
            # 当前价已跌破颈线或在此附近测试 (颈线上方 1.5% 以内或下方)
            price_broken = current_price <= v * 1.015

            if peaks_close and valley_deep and price_broken:
                results['m_top'] = True
                results['m_top_date'] = str(e3['date'].date()) if hasattr(e3['date'], 'date') else str(e3['date'])
                results['m_top_peaks'] = (round(p1, 2), round(p2, 2))
                results['m_top_neckline'] = round(v, 2)

    # 检测 W底: Valley1 -> Peak -> Valley2 模式，且当前价格已突破或极为接近颈线 (Peak)
    for i in range(len(extrema) - 2):
        e1, e2, e3 = extrema[i], extrema[i+1], extrema[i+2]
        if e1['type'] == 'valley' and e2['type'] == 'peak' and e3['type'] == 'valley':
            v1, p, v2 = e1['val'], e2['val'], e3['val']

            # 两个底相差不超过 4%
            valleys_close = abs(v1 - v2) / max(v1, v2) <= 0.04
            # 颈线高度大于 3% (即低点反弹幅度)
            peak_high = (p - max(v1, v2)) / max(v1, v2) >= 0.03
            # 当前价已突破颈线或在此附近测试 (颈线下方 1.5% 以内或上方)
            price_broken = current_price >= p * 0.985

            if valleys_close and peak_high and price_broken:
                results['w_bottom'] = True
                results['w_bottom_date'] = str(e3['date'].date()) if hasattr(e3['date'], 'date') else str(e3['date'])
                results['w_bottom_valleys'] = (round(v1, 2), round(v2, 2))
                results['w_bottom_neckline'] = round(p, 2)

    return results


def detect_head_shoulders(df: pd.DataFrame) -> Dict[str, Any]:
    """
    侦测头肩顶与头肩底反转结构（左肩、头部、右肩及颈线检测）。
    """
    closes = df['Close'].tolist()
    dates = df.index.tolist()
    n = len(closes)

    results = {
        'head_shoulders': False,
        'head_shoulders_date': None,
        'head_shoulders_type': None,  # 'top' 或 'bottom'
        'head_shoulders_values': None,  # (左肩, 头部, 右肩)
        'head_shoulders_neckline': None
    }

    if n < 30:
        return results

    window = min(90, n)
    recent_closes = closes[-window:]
    recent_dates = dates[-window:]

    # 寻找局部极值
    extrema = []
    for i in range(1, window - 1):
        if recent_closes[i] > recent_closes[i-1] and recent_closes[i] > recent_closes[i+1]:
            extrema.append({'type': 'peak', 'val': recent_closes[i], 'idx': i, 'date': recent_dates[i]})
        elif recent_closes[i] < recent_closes[i-1] and recent_closes[i] < recent_closes[i+1]:
            extrema.append({'type': 'valley', 'val': recent_closes[i], 'idx': i, 'date': recent_dates[i]})

    current_price = closes[-1]

    # 检测头肩顶: Peak1 (左肩) -> Valley1 -> Peak2 (头部) -> Valley2 -> Peak3 (右肩)
    for i in range(len(extrema) - 4):
        e1, e2, e3, e4, e5 = extrema[i], extrema[i+1], extrema[i+2], extrema[i+3], extrema[i+4]
        if (e1['type'] == 'peak' and e2['type'] == 'valley' and
            e3['type'] == 'peak' and e4['type'] == 'valley' and e5['type'] == 'peak'):

            ls, head, rs = e1['val'], e3['val'], e5['val']
            v1, v2 = e2['val'], e4['val']

            # 头部高度显著高于两侧肩膀（至少高 1.5%）
            head_highest = head > ls * 1.015 and head > rs * 1.015
            # 两侧肩膀高度相近（相差 4% 以内）
            shoulders_close = abs(ls - rs) / max(ls, rs) <= 0.04
            # 颈线取两个谷值的较大值作为近似水平颈线
            neckline = max(v1, v2)
            # 当前价格跌破或极度接近颈线（颈线上方 1.5% 以内或已跌破）
            price_broken = current_price <= neckline * 1.015

            if head_highest and shoulders_close and price_broken:
                results['head_shoulders'] = True
                results['head_shoulders_date'] = str(e5['date'].date()) if hasattr(e5['date'], 'date') else str(e5['date'])
                results['head_shoulders_type'] = 'top'
                results['head_shoulders_values'] = (round(ls, 2), round(head, 2), round(rs, 2))
                results['head_shoulders_neckline'] = round(neckline, 2)

    # 检测头肩底 (倒头肩): Valley1 (左肩) -> Peak1 -> Valley2 (头部) -> Peak2 -> Valley3 (右肩)
    for i in range(len(extrema) - 4):
        e1, e2, e3, e4, e5 = extrema[i], extrema[i+1], extrema[i+2], extrema[i+3], extrema[i+4]
        if (e1['type'] == 'valley' and e2['type'] == 'peak' and
            e3['type'] == 'valley' and e4['type'] == 'peak' and e5['type'] == 'valley'):

            ls, head, rs = e1['val'], e3['val'], e5['val']
            p1, p2 = e2['val'], e4['val']

            # 头部底点显著低于两侧肩膀底点（至少低 1.5%）
            head_lowest = head < ls * 0.985 and head < rs * 0.985
            # 两侧肩膀底点相近（相差 4% 以内）
            shoulders_close = abs(ls - rs) / max(ls, rs) <= 0.04
            # 颈线取两个峰值的较小值作为近似阻力颈线
            neckline = min(p1, p2)
            # 当前价格突破或极为接近颈线（颈线下方 1.5% 以内或已突破）
            price_broken = current_price >= neckline * 0.985

            if head_lowest and shoulders_close and price_broken:
                results['head_shoulders'] = True
                results['head_shoulders_date'] = str(e5['date'].date()) if hasattr(e5['date'], 'date') else str(e5['date'])
                results['head_shoulders_type'] = 'bottom'
                results['head_shoulders_values'] = (round(ls, 2), round(head, 2), round(rs, 2))
                results['head_shoulders_neckline'] = round(neckline, 2)

    return results


def detect_triple_patterns(df: pd.DataFrame) -> Dict[str, Any]:
    """
    侦测三重顶与三重底结构。
    """
    closes = df['Close'].tolist()
    dates = df.index.tolist()
    n = len(closes)

    results = {
        'triple_top': False,
        'triple_top_date': None,
        'triple_top_peaks': None,
        'triple_top_neckline': None,
        'triple_bottom': False,
        'triple_bottom_date': None,
        'triple_bottom_valleys': None,
        'triple_bottom_neckline': None
    }

    if n < 25:
        return results

    window = min(60, n)
    recent_closes = closes[-window:]
    recent_dates = dates[-window:]

    # 寻找局部极值
    extrema = []
    for i in range(1, window - 1):
        if recent_closes[i] > recent_closes[i-1] and recent_closes[i] > recent_closes[i+1]:
            extrema.append({'type': 'peak', 'val': recent_closes[i], 'idx': i, 'date': recent_dates[i]})
        elif recent_closes[i] < recent_closes[i-1] and recent_closes[i] < recent_closes[i+1]:
            extrema.append({'type': 'valley', 'val': recent_closes[i], 'idx': i, 'date': recent_dates[i]})

    current_price = closes[-1]

    # 检测三重顶: Peak1 -> Valley1 -> Peak2 -> Valley2 -> Peak3
    for i in range(len(extrema) - 4):
        e1, e2, e3, e4, e5 = extrema[i], extrema[i+1], extrema[i+2], extrema[i+3], extrema[i+4]
        if (e1['type'] == 'peak' and e2['type'] == 'valley' and
            e3['type'] == 'peak' and e4['type'] == 'valley' and e5['type'] == 'peak'):

            p1, p2, p3 = e1['val'], e3['val'], e5['val']
            v1, v2 = e2['val'], e4['val']

            # 三个顶最高与最低差异在 3.5% 以内
            peaks_close = (max(p1, p2, p3) - min(p1, p2, p3)) / max(p1, p2, p3) <= 0.035
            # 颈线为两谷值之较小者
            neckline = min(v1, v2)
            # 当前价已跌破颈线或在此附近测试 (颈线上方 1.5% 以内或下方)
            price_broken = current_price <= neckline * 1.015

            if peaks_close and price_broken:
                results['triple_top'] = True
                results['triple_top_date'] = str(e5['date'].date()) if hasattr(e5['date'], 'date') else str(e5['date'])
                results['triple_top_peaks'] = (round(p1, 2), round(p2, 2), round(p3, 2))
                results['triple_top_neckline'] = round(neckline, 2)

    # 检测三重底: Valley1 -> Peak1 -> Valley2 -> Peak2 -> Valley3
    for i in range(len(extrema) - 4):
        e1, e2, e3, e4, e5 = extrema[i], extrema[i+1], extrema[i+2], extrema[i+3], extrema[i+4]
        if (e1['type'] == 'valley' and e2['type'] == 'peak' and
            e3['type'] == 'valley' and e4['type'] == 'peak' and e5['type'] == 'valley'):

            v1, v2, v3 = e1['val'], e3['val'], e5['val']
            p1, p2 = e2['val'], e4['val']

            # 三个底最大与最小差异在 3.5% 以内
            valleys_close = (max(v1, v2, v3) - min(v1, v2, v3)) / max(v1, v2, v3) <= 0.035
            # 颈线为两峰值之较大者
            neckline = max(p1, p2)
            # 当前价已突破颈线或在此附近测试 (颈线下方 1.5% 以内或上方)
            price_broken = current_price >= neckline * 0.985

            if valleys_close and price_broken:
                results['triple_bottom'] = True
                results['triple_bottom_date'] = str(e5['date'].date()) if hasattr(e5['date'], 'date') else str(e5['date'])
                results['triple_bottom_valleys'] = (round(v1, 2), round(v2, 2), round(v3, 2))
                results['triple_bottom_neckline'] = round(neckline, 2)

    return results


def _calc_rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """计算整个收盘价序列的 Wilder RSI 序列（与 utils.market_metrics.py 算法完全对齐）"""
    n = len(closes)
    rsi_series = [None] * n
    if n < period + 1:
        return rsi_series

    delta = [closes[k] - closes[k-1] for k in range(1, n)]
    gain = [d if d > 0 else 0.0 for d in delta]
    loss = [-d if d < 0 else 0.0 for d in delta]

    # 第一个周期以 SMA 启动
    avg_gain = sum(gain[:period]) / period
    avg_loss = sum(loss[:period]) / period

    if avg_loss == 0:
        rsi_series[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_series[period] = 100.0 - (100.0 / (1.0 + rs))

    # Wilder RMA 递归平滑
    for k in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gain[k-1]) / period
        avg_loss = (avg_loss * (period - 1) + loss[k-1]) / period

        if avg_loss == 0:
            rsi_series[k] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_series[k] = 100.0 - (100.0 / (1.0 + rs))

    return rsi_series


def detect_rsi_divergence(df: pd.DataFrame, symbol: str = "") -> Dict[str, Any]:
    """
    侦测价格与 RSI 指标的顶背离与底背离。
    顶背离：价格新高但 RSI 新低。
    底背离：价格新低但 RSI 新高。
    """
    closes = df['Close'].tolist()
    dates = df.index.tolist()
    n = len(closes)

    results = {
        'bearish_divergence': False,
        'bearish_divergence_date': None,
        'bearish_divergence_details': None,
        'bullish_divergence': False,
        'bullish_divergence_date': None,
        'bullish_divergence_details': None
    }

    if n < 35:
        return results

    # Load dynamic rsi parameters
    rsi_ob = _get_param(symbol, "rsi_overbought")
    rsi_os = _get_param(symbol, "rsi_oversold")

    # 计算全局 RSI
    rsi_series = _calc_rsi_series(closes, period=14)

    # 取最近 40 天进行背离扫描
    window = min(40, n)
    recent_closes = closes[-window:]
    recent_rsi = rsi_series[-window:]
    recent_dates = dates[-window:]

    # 寻找价格的高点和低点
    peaks = []
    valleys = []
    for i in range(1, window - 1):
        if recent_rsi[i] is None:
            continue
        # 价格局部峰值
        if recent_closes[i] > recent_closes[i-1] and recent_closes[i] > recent_closes[i+1]:
            peaks.append({
                'idx': i,
                'price': recent_closes[i],
                'rsi': recent_rsi[i],
                'date': recent_dates[i]
            })
        # 价格局部谷值
        if recent_closes[i] < recent_closes[i-1] and recent_closes[i] < recent_closes[i+1]:
            valleys.append({
                'idx': i,
                'price': recent_closes[i],
                'rsi': recent_rsi[i],
                'date': recent_dates[i]
            })

    # 顶背离：后一个峰值价格 > 前一个，但对应的 RSI 后一个 < 前一个
    if len(peaks) >= 2:
        for idx in range(len(peaks) - 1):
            p1 = peaks[idx]
            p2 = peaks[idx+1]

            # 价格上涨 (至少涨 1%) 且 RSI 下跌 (至少降 2)
            price_rising = p2['price'] > p1['price'] * 1.01
            rsi_falling = p2['rsi'] < p1['rsi'] - 2.0
            # 要求最近的峰值发生在 15 个交易日以内
            is_recent = (window - p2['idx']) <= 15
            rsi_ob_check = (p1['rsi'] > rsi_ob or p2['rsi'] > rsi_ob)

            if price_rising and rsi_falling and is_recent and rsi_ob_check:
                results['bearish_divergence'] = True
                results['bearish_divergence_date'] = str(p2['date'].date()) if hasattr(p2['date'], 'date') else str(p2['date'])
                results['bearish_divergence_details'] = (
                    f"价格峰值: {p1['price']:.2f} -> {p2['price']:.2f} (升高), "
                    f"RSI 峰值: {p1['rsi']:.1f} -> {p2['rsi']:.1f} (降低)"
                )

    # 底背离：后一个谷值价格 < 前一个，但对应的 RSI 后一个 > 前一个
    if len(valleys) >= 2:
        for idx in range(len(valleys) - 1):
            v1 = valleys[idx]
            v2 = valleys[idx+1]

            # 价格下跌 (至少跌 1%) 且 RSI 上涨 (至少涨 2)
            price_falling = v2['price'] < v1['price'] * 0.99
            rsi_rising = v2['rsi'] > v1['rsi'] + 2.0
            is_recent = (window - v2['idx']) <= 15

            if price_falling and rsi_rising and is_recent:
                results['bullish_divergence'] = True
                results['bullish_divergence_date'] = str(v2['date'].date()) if hasattr(v2['date'], 'date') else str(v2['date'])
                results['bullish_divergence_details'] = (
                    f"价格谷值: {v1['price']:.2f} -> {v2['price']:.2f} (降低), "
                    f"RSI 谷值: {v1['rsi']:.1f} -> {v2['rsi']:.1f} (升高)"
                )

    return results


def detect_gaps(df: pd.DataFrame, symbol: str = "") -> List[Dict[str, Any]]:
    """
    寻找最近 10 个交易日内的跳空缺口，并检测是否已被后续 K 线回补。
    """
    gaps = []
    if "High" not in df.columns or "Low" not in df.columns or len(df) < 11:
        return gaps

    closes = df['Close'].tolist()
    highs = df['High'].tolist()
    lows = df['Low'].tolist()
    dates = df.index.tolist()

    gap_pct = _get_param(symbol, "gap_threshold_pct")
    trend_w = int(_get_param(symbol, "trend_window"))

    ma_trend = None
    if len(df) >= trend_w:
        ma_trend = df['Close'].rolling(window=trend_w).mean().tolist()

    lookback = 10
    n = len(closes)
    for i in range(n - lookback, n):
        prev_close = closes[i-1]
        prev_high = highs[i-1]
        prev_low = lows[i-1]
        curr_high = highs[i]
        curr_low = lows[i]
        curr_date = dates[i]

        threshold = prev_close * gap_pct

        # 向上跳空: 今日 Low > 昨日 High + threshold
        if curr_low > prev_high + threshold:
            if ma_trend is not None and ma_trend[i] is not None and not pd.isna(ma_trend[i]):
                if closes[i] <= ma_trend[i]:
                    continue

            # 判断后续是否回补 (即后续某日的 Low <= 昨日 High)
            filled = False
            fill_date = None
            for j in range(i + 1, n):
                if lows[j] <= prev_high:
                    filled = True
                    fill_date = str(dates[j].date()) if hasattr(dates[j], 'date') else str(dates[j])
                    break
            gaps.append({
                'type': 'up',
                'date': str(curr_date.date()) if hasattr(curr_date, 'date') else str(curr_date),
                'gap_range': (round(prev_high, 2), round(curr_low, 2)),
                'filled': filled,
                'fill_date': fill_date,
                'days_ago': n - 1 - i
            })

        # 向下跳空: 今日 High < 昨日 Low - threshold
        elif curr_high < prev_low - threshold:
            if ma_trend is not None and ma_trend[i] is not None and not pd.isna(ma_trend[i]):
                if closes[i] >= ma_trend[i]:
                    continue

            # 判断后续是否回补 (即后续某日的 High >= 昨日 Low)
            filled = False
            fill_date = None
            for j in range(i + 1, n):
                if highs[j] >= prev_low:
                    filled = True
                    fill_date = str(dates[j].date()) if hasattr(dates[j], 'date') else str(dates[j])
                    break
            gaps.append({
                'type': 'down',
                'date': str(curr_date.date()) if hasattr(curr_date, 'date') else str(curr_date),
                'gap_range': (round(curr_high, 2), round(prev_low, 2)),
                'filled': filled,
                'fill_date': fill_date,
                'days_ago': n - 1 - i
            })

    return gaps


def _get_limit_pct(symbol: str) -> float:
    """A股各板块涨跌停比例"""
    sym = (symbol or "").strip()
    if sym.startswith(("300", "301", "688", "689")):
        return 20.0
    elif sym.startswith(("8", "4")):
        return 30.0
    else:
        return 10.0


def detect_failed_limit_up(df: pd.DataFrame, symbol: str, name: str = "") -> List[Dict[str, Any]]:
    """
    A股封板失败检测。
    检测最近 15 个交易日内，当日最高价触及或超过理论涨停价，但收盘未能封死涨停的交易日记录。
    """
    failed_events = []
    if "High" not in df.columns or len(df) < 2:
        return []

    closes = df['Close'].tolist()
    highs = df['High'].tolist()
    volumes = df['Volume'].tolist() if 'Volume' in df.columns else None
    dates = df.index.tolist()

    vol_mult = _get_param(symbol, "failed_limit_up_vol_mult")
    shadow_pct = _get_param(symbol, "failed_limit_up_shadow_pct")
    trend_w = int(_get_param(symbol, "trend_window"))

    vol_ma20 = df['Volume'].rolling(window=20).mean().tolist() if 'Volume' in df.columns else None
    ma_trend = df['Close'].rolling(window=trend_w).mean().tolist()

    # 确定涨跌幅限制比例
    is_st = "ST" in (name or "").upper()
    limit_pct = _get_limit_pct(symbol)
    if is_st and limit_pct == 10.0:
        limit_pct = 5.0

    lookback = min(15, len(closes) - 1)
    for i in range(len(closes) - lookback, len(closes)):
        prev_close = closes[i-1]
        curr_high = highs[i]
        curr_close = closes[i]
        curr_date = dates[i]

        # 理论涨停价 (四舍五入保留2位)
        limit_price = round(prev_close * (1 + limit_pct / 100.0), 2)

        # A股由于四舍五入，最高价触及 limit_price - 0.005 以上均算触及
        touched = curr_high >= limit_price - 0.005
        # 收盘未能收在 limit_price - 0.005 以上
        failed_to_close = curr_close < limit_price - 0.005

        if touched and failed_to_close:
            vol_ok = True
            if volumes is not None and vol_ma20 is not None and vol_ma20[i] is not None and not pd.isna(vol_ma20[i]):
                vol_ok = volumes[i] >= vol_ma20[i] * vol_mult

            shadow_ok = (curr_high - curr_close) / curr_close >= shadow_pct

            not_too_strong = True
            if ma_trend is not None and ma_trend[i] is not None and not pd.isna(ma_trend[i]):
                if curr_close > ma_trend[i] * 1.15:
                    not_too_strong = False

            if vol_ok and shadow_ok and not_too_strong:
                change_pct = (curr_close / prev_close - 1) * 100.0
                failed_events.append({
                    'date': str(curr_date.date()) if hasattr(curr_date, 'date') else str(curr_date),
                    'high': round(curr_high, 2),
                    'close': round(curr_close, 2),
                    'limit_price': limit_price,
                    'change_pct': round(change_pct, 2),
                    'days_ago': len(closes) - 1 - i
                })

    return failed_events


def analyze_chan(df: pd.DataFrame, symbol: str, name: str = "") -> Dict[str, Any]:
    """
    综合执行缠论和技术形态分析。
    """
    merged = remove_inclusions(df)
    fractals = find_fractals(merged)
    bis = build_bi(merged, fractals)
    zhongshus = build_zhongshu(bis)
    patterns = detect_double_patterns(df)
    failed_limit_ups = detect_failed_limit_up(df, symbol, name)

    # 扩展形态检测
    hs_patterns = detect_head_shoulders(df)
    triple_patterns = detect_triple_patterns(df)
    rsi_div = detect_rsi_divergence(df, symbol)
    gaps = detect_gaps(df, symbol)

    return {
        'merged_count': len(merged),
        'fractals_count': len(fractals),
        'bis': bis,
        'zhongshus': zhongshus,
        'patterns': patterns,
        'failed_limit_ups': failed_limit_ups,
        'hs_patterns': hs_patterns,
        'triple_patterns': triple_patterns,
        'rsi_divergence': rsi_div,
        'gaps': gaps,
        'symbol': symbol
    }


def _get_accuracy_str(symbol: str, pattern_key: str) -> str:
    """获取指定标的在指定技术形态上的历史正确率"""
    defaults = {
        "upward_gap": {"correct": 16, "total": 28, "accuracy": 57.1},
        "downward_gap": {"correct": 8, "total": 18, "accuracy": 44.4},
        "failed_limit_up": {"correct": 2, "total": 4, "accuracy": 50.0},
        "rsi_bearish": {"correct": 1, "total": 1, "accuracy": 100.0},
        "rsi_bullish": {"correct": 1, "total": 1, "accuracy": 100.0},
        "w_bottom": {"correct": 14, "total": 24, "accuracy": 58.3},
        "m_top": {"correct": 11, "total": 20, "accuracy": 55.0},
        "head_shoulders_top": {"correct": 15, "total": 25, "accuracy": 60.0},
        "head_shoulders_bottom": {"correct": 18, "total": 29, "accuracy": 62.1},
        "triple_top": {"correct": 9, "total": 16, "accuracy": 56.2},
        "triple_bottom": {"correct": 10, "total": 17, "accuracy": 58.8},
    }

    params = load_params()
    symbol_str = str(symbol).strip()

    if symbol_str in params and "accuracy_meta" in params[symbol_str]:
        meta = params[symbol_str]["accuracy_meta"]
        if pattern_key in meta:
            corr = meta[pattern_key].get("correct", 0)
            tot = meta[pattern_key].get("total", 0)
            acc = meta[pattern_key].get("accuracy", 0.0)
            if tot > 0:
                return f"[历史正确率: {acc:.1f}% ({corr}/{tot})]"
            else:
                return "[历史正确率: 暂无触发记录]"

    if "default_accuracy" in params and pattern_key in params["default_accuracy"]:
        d_acc = params["default_accuracy"][pattern_key]
        corr = d_acc.get("correct", 0)
        tot = d_acc.get("total", 0)
        acc = d_acc.get("accuracy", 0.0)
        return f"[历史正确率: {acc:.1f}% ({corr}/{tot})]"

    if pattern_key in defaults:
        d_acc = defaults[pattern_key]
        corr = d_acc.get("correct", 0)
        tot = d_acc.get("total", 0)
        acc = d_acc.get("accuracy", 0.0)
        return f"[历史正确率: {acc:.1f}% ({corr}/{tot})]"

    return ""


def _get_pattern_accuracy(symbol: str, pattern_key: str) -> float:
    import re
    acc_str = _get_accuracy_str(symbol, pattern_key)
    match = re.search(r"历史正确率:\s*([\d.]+)%", acc_str)
    if match:
        return float(match.group(1))

    # Fallback default values
    defaults = {
        "upward_gap": 57.1,
        "downward_gap": 44.4,
        "failed_limit_up": 50.0,
        "rsi_bearish": 100.0,
        "rsi_bullish": 100.0,
        "w_bottom": 58.3,
        "m_top": 55.0,
        "head_shoulders_top": 60.0,
        "head_shoulders_bottom": 62.1,
        "triple_top": 56.2,
        "triple_bottom": 58.8,
    }
    return defaults.get(pattern_key, 0.0)


def format_chan_brief(chan_data: Dict[str, Any]) -> str:
    """
    将缠论和技术形态分析转化为易于 LLM 阅读的技术简报。
    """
    lines = []
    symbol = chan_data.get('symbol', '')

    # 1. 笔分析
    bis = chan_data.get('bis', [])
    if bis:
        last_bi = bis[-1]
        lines.append(f"- 最新确认笔: {'向上 ↗' if last_bi['type'] == 'up' else '向下 ↘'} "
                     f"(起点: {last_bi['start_date']} 的 {last_bi['start_val']:.2f} -> "
                     f"终点: {last_bi['end_date']} 的 {last_bi['end_val']:.2f})")
    else:
        lines.append("- 最新确认笔: 暂无确立的笔 (历史波动未满足缠论笔定义)")

    # 2. 中枢分析
    zhongshus = chan_data.get('zhongshus', [])
    if zhongshus:
        last_zs = zhongshus[-1]
        lines.append(f"- 最新确认中枢: 价格区间 [{last_zs['zd']:.2f}, {last_zs['zg']:.2f}] "
                     f"(时间跨度: {last_zs['start_date']} 至 {last_zs['end_date']})")
    else:
        lines.append("- 最新中枢状态: 暂无确认的缠论中枢")

    # 3. 收集所有触发的信号
    signals = []
    patterns = chan_data.get('patterns', {})
    hs = chan_data.get('hs_patterns', {})
    triple = chan_data.get('triple_patterns', {})
    div = chan_data.get('rsi_divergence', {})
    gaps = chan_data.get('gaps', [])
    failed_lu = chan_data.get('failed_limit_ups', [])

    # 双底
    if patterns.get('w_bottom'):
        valleys = patterns['w_bottom_valleys']
        acc = _get_pattern_accuracy(symbol, 'w_bottom')
        acc_str = _get_accuracy_str(symbol, 'w_bottom')
        signals.append({
            "accuracy": acc,
            "text": f"- 形态警示: ✅ 侦测到【W底】(双底) 结构！ (低点: {valleys[0]:.2f} 与 {valleys[1]:.2f}，颈线: {patterns['w_bottom_neckline']:.2f}，触发时间: {patterns['w_bottom_date']}) {acc_str}",
            "prediction": "- 走势预测: 看多 ↗，筑底完成，可能突破颈线并开启/延续上涨走势。"
        })

    # 双顶
    if patterns.get('m_top'):
        peaks = patterns['m_top_peaks']
        acc = _get_pattern_accuracy(symbol, 'm_top')
        acc_str = _get_accuracy_str(symbol, 'm_top')
        signals.append({
            "accuracy": acc,
            "text": f"- 形态警示: 🔴 侦测到【M顶】(双顶) 结构！ (高点: {peaks[0]:.2f} 与 {peaks[1]:.2f}，颈线: {patterns['m_top_neckline']:.2f}，触发时间: {patterns['m_top_date']}) {acc_str}",
            "prediction": "- 走势预测: 看空 ↘，双顶筑顶，可能跌破颈线并开启/延续下跌走势。"
        })

    # 头肩底/头肩顶
    if hs.get('head_shoulders'):
        hs_type = hs['head_shoulders_type']
        vals = hs['head_shoulders_values']
        neckline = hs['head_shoulders_neckline']
        if hs_type == 'top':
            acc = _get_pattern_accuracy(symbol, 'head_shoulders_top')
            acc_str = _get_accuracy_str(symbol, 'head_shoulders_top')
            signals.append({
                "accuracy": acc,
                "text": f"- 形态警示: 🔴 侦测到【头肩顶】反转结构！ (左肩: {vals[0]:.2f}，头部: {vals[1]:.2f}，右肩: {vals[2]:.2f}，颈线: {neckline:.2f}，触发时间: {hs['head_shoulders_date']}) {acc_str}",
                "prediction": "- 走势预测: 看空 ↘，头肩顶强反转信号，筑顶完成，后续大概率破位下行。"
            })
        else:
            acc = _get_pattern_accuracy(symbol, 'head_shoulders_bottom')
            acc_str = _get_accuracy_str(symbol, 'head_shoulders_bottom')
            signals.append({
                "accuracy": acc,
                "text": f"- 形态警示: ✅ 侦测到【头肩底】(倒头肩) 结构！ (左肩: {vals[0]:.2f}，头部: {vals[1]:.2f}，右肩: {vals[2]:.2f}，颈线: {neckline:.2f}，触发时间: {hs['head_shoulders_date']}) {acc_str}",
                "prediction": "- 走势预测: 看多 ↗，头肩底强反转信号，后续大概率见底反弹上行。"
            })

    # 三重顶/底
    if triple.get('triple_top'):
        peaks = triple['triple_top_peaks']
        acc = _get_pattern_accuracy(symbol, 'triple_top')
        acc_str = _get_accuracy_str(symbol, 'triple_top')
        signals.append({
            "accuracy": acc,
            "text": f"- 形态警示: 🔴 侦测到【三重顶】结构！ (高点: {peaks[0]:.2f}, {peaks[1]:.2f}, {peaks[2]:.2f}，颈线: {triple['triple_top_neckline']:.2f}，触发时间: {triple['triple_top_date']}) {acc_str}",
            "prediction": "- 走势预测: 看空 ↘，三重顶阻力极强，筑顶成功，后续偏向回撤下跌。"
        })

    if triple.get('triple_bottom'):
        valleys = triple['triple_bottom_valleys']
        acc = _get_pattern_accuracy(symbol, 'triple_bottom')
        acc_str = _get_accuracy_str(symbol, 'triple_bottom')
        signals.append({
            "accuracy": acc,
            "text": f"- 形态警示: ✅ 侦测到【三重底】结构！ (低点: {valleys[0]:.2f}, {valleys[1]:.2f}, {valleys[2]:.2f}，颈线: {triple['triple_bottom_neckline']:.2f}，触发时间: {triple['triple_bottom_date']}) {acc_str}",
            "prediction": "- 走势预测: 看多 ↗，三重底支撑极强，筑底成功，后续偏向反弹上行。"
        })

    # RSI 价量背离
    if div.get('bearish_divergence'):
        acc = _get_pattern_accuracy(symbol, 'rsi_bearish')
        acc_str = _get_accuracy_str(symbol, 'rsi_bearish')
        signals.append({
            "accuracy": acc,
            "text": f"- 指标背离: 🔴 侦测到【RSI 顶背离】(多头动能衰竭信号)！ {acc_str} ({div['bearish_divergence_details']}，触发时间: {div['bearish_divergence_date']})",
            "prediction": "- 走势预测: 看空 ↘，RSI顶背离示多头动能衰竭，短期内大概率见顶回调。"
        })
    if div.get('bullish_divergence'):
        acc = _get_pattern_accuracy(symbol, 'rsi_bullish')
        acc_str = _get_accuracy_str(symbol, 'rsi_bullish')
        signals.append({
            "accuracy": acc,
            "text": f"- 指标背离: ✅ 侦测到【RSI 底背离】(空头动能衰竭信号)！ {acc_str} ({div['bullish_divergence_details']}，触发时间: {div['bullish_divergence_date']})",
            "prediction": "- 走势预测: 看多 ↗，RSI底背离示空头动能衰竭，短期内大概率止跌企稳并反弹。"
        })

    # 跳空缺口 (取最新一个)
    if gaps:
        g = gaps[0]
        acc_key = "upward_gap" if g['type'] == 'up' else "downward_gap"
        acc = _get_pattern_accuracy(symbol, acc_key)
        acc_str = _get_accuracy_str(symbol, acc_key)
        filled_str = f"已回补 (回补时间: {g['fill_date']})" if g['filled'] else "未回补 (强力支撑/阻力)"
        if g['type'] == 'up':
            signals.append({
                "accuracy": acc,
                "text": f"- 缺口监测: ✅ 侦测到【向上跳空缺口】！ {acc_str} ({g['date']} 缺口区间 [{g['gap_range'][0]:.2f}, {g['gap_range'][1]:.2f}] ({filled_str}))",
                "prediction": "- 走势预测: 看多 ↗，向上跳空缺口彰显多头强势，未回补前为强支撑，大概率震荡上行。"
            })
        else:
            signals.append({
                "accuracy": acc,
                "text": f"- 缺口监测: 🔴 侦测到【向下跳空缺口】！ {acc_str} ({g['date']} 缺口区间 [{g['gap_range'][0]:.2f}, {g['gap_range'][1]:.2f}] ({filled_str}))",
                "prediction": "- 走势预测: 看空 ↘，向下跳空缺口彰显空头强势，未回补前为强阻力，大概率震荡下行。"
            })

    # 封板失败 (取最新一个)
    if failed_lu:
        ev = failed_lu[0]
        acc = _get_pattern_accuracy(symbol, 'failed_limit_up')
        acc_str = _get_accuracy_str(symbol, 'failed_limit_up')
        signals.append({
            "accuracy": acc,
            "text": f"- 封板异常: ⚠️ 侦测到【封板失败】冲高回落！ {acc_str} ({ev['date']} 理论涨停价 {ev['limit_price']:.2f}，日内最高触及 {ev['high']:.2f}，收盘回落至 {ev['close']:.2f})",
            "prediction": "- 走势预测: 看空 ↘，封板失败冲高回落，说明上方卖压沉重且买盘力竭，短期偏向回调洗盘。"
        })

    # 筛选出正确率最高的信号进行单独呈现
    if signals:
        # 按照正确率降序排列
        signals.sort(key=lambda s: s["accuracy"], reverse=True)
        best = signals[0]
        lines.append(best["text"])
        lines.append(best["prediction"])
    else:
        lines.append("- 形态提示: 暂未发现显著的技术形态与指标信号。")

    return "\n".join(lines)
