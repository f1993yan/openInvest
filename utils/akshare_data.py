"""akshare 数据层 — 替代 yfinance，使用国内数据源

提供与 exchange_fee.py 兼容的接口：
- get_history_data(symbol, period, as_of_date=None) → pd.DataFrame
- get_macro_data() → str（格式化宏观指标文本）
- get_macro_snapshot() → dict（结构化宏观快照，供 agents/tools.py 用）

信息源：akshare（东方财富/新浪财经/同花顺等国内数据源聚合）

设计原则：
- 返回 DataFrame 列名与现有 market_metrics.py 兼容（Open/High/Low/Close/Volume）
- 接口签名与 exchange_fee.py 一致，上层 committee_runner 零改动
- 所有网络异常 graceful 降级为空 DataFrame，不阻断主流程
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from db.market_store import MarketStore

log = logging.getLogger(__name__)
_STORE = MarketStore()
_CACHE_MAX_STALE_DAYS = int(os.getenv("INVEST_AKSHARE_HISTORY_CACHE_STALE_DAYS", "3"))
os.environ.setdefault("TQDM_DISABLE", "1")

# ==========================================
# 内部：akshare 行情拉取
# ==========================================


def _parse_period_to_start(period: str) -> str:
    """把 yfinance 风格的 period 转成 YYYYMMDD 起始日期"""
    days_map = {
        "1d": 1, "5d": 7, "1mo": 30, "3mo": 90,
        "6mo": 180, "1y": 365, "2y": 730,
    }
    days = days_map.get(period, 365)
    return (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")


def _period_to_days(period: str) -> int:
    days_map = {
        "1d": 1, "5d": 7, "1mo": 30, "3mo": 90,
        "6mo": 180, "1y": 365, "2y": 730,
    }
    return days_map.get(period, 365)


def _apply_period_filter(df: pd.DataFrame, period: str) -> pd.DataFrame:
    if df.empty:
        return df
    start_dt = pd.to_datetime(_parse_period_to_start(period))
    return df[df.index >= start_dt]


def _apply_cutoff(df: pd.DataFrame, as_of_date: Optional[str]) -> pd.DataFrame:
    if not as_of_date or df.empty:
        return df
    try:
        cutoff = pd.to_datetime(as_of_date)
        return df[df.index < cutoff]
    except Exception:
        return df


def _cache_is_fresh(df: pd.DataFrame) -> bool:
    """Daily OHLC cache freshness.

    Akshare daily bars do not need to be refreshed every 30-minute monitor round.
    A small calendar-day tolerance handles weekends and holidays while still
    letting the cache refresh after a real stale gap.
    """
    if df.empty:
        return False
    if len(df) < 10:
        # Only a few cached rows are not enough for daily/weekly/monthly trend features.
        return False
    try:
        latest = pd.to_datetime(df.index[-1]).date()
        age_days = (datetime.now().date() - latest).days
        return age_days <= _CACHE_MAX_STALE_DAYS
    except Exception:
        return False


def _save_to_cache(symbol: str, df: pd.DataFrame, source: str = "akshare") -> None:
    if df.empty:
        return
    for idx, row in df.iterrows():
        try:
            close = row.get("Close")
            if pd.isna(close):
                continue
            _STORE.save_generic_price(
                symbol,
                pd.to_datetime(idx).strftime("%Y-%m-%d"),
                float(close),
                source=source,
                high=None if pd.isna(row.get("High")) else float(row.get("High")),
                low=None if pd.isna(row.get("Low")) else float(row.get("Low")),
                volume=None if pd.isna(row.get("Volume")) else float(row.get("Volume")),
            )
        except Exception as e:  # noqa: BLE001
            log.debug(f"akshare cache save skip {symbol}: {e}")


def _map_sina_columns(df: pd.DataFrame) -> pd.DataFrame:
    """新浪数据源列名映射（stock_zh_a_daily / stock_hk_daily）
    返回小写列名：date→index, open/high/low/close/volume 保留
    """
    # 新浪源列名已是英文小写：date, open, high, low, close, volume
    # 确保 date 列存在并设为索引
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        df.index.name = "Date"
    # 标准化列名首字母大写
    col_map = {
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col not in df.columns:
            df[col] = np.nan
    return df


def _map_akshare_columns(df: pd.DataFrame) -> pd.DataFrame:
    """akshare DataFrame 列名映射为 Open/High/Low/Close/Volume 标准格式"""
    col_map = {
        "开盘": "Open",
        "最高": "High",
        "最低": "Low",
        "收盘": "Close",
        "成交量": "Volume",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    # 日期列设为索引
    for date_col in ["日期", "date", "trade_date"]:
        if date_col in df.columns:
            df[date_col] = pd.to_datetime(df[date_col])
            df = df.set_index(date_col)
            df.index.name = "Date"
            break
    # 确保有必需的列
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col not in df.columns:
            df[col] = np.nan
    return df


def _is_a_share(symbol: str) -> bool:
    """判断是否是 A 股代码（6 位纯数字）"""
    return symbol.isdigit() and len(symbol) == 6


def _is_hk_stock(symbol: str) -> bool:
    """判断是否是港股代码（5 位数字，可能带前导零）"""
    return symbol.isdigit() and len(symbol) == 5


def _a_share_prefix(symbol: str) -> str:
    """判断 A 股代码的交易所前缀"""
    first = symbol[0]
    # 深市：0/3 开头（000xxx-004xxx 主板，300xxx 创业板，002xxx 中小板）
    # 沪市：6 开头（600xxx/601xxx/603xxx/605xxx 主板，688xxx 科创板）
    if first in ("0", "2", "3"):
        return f"sz{symbol}"
    if first == "6":
        return f"sh{symbol}"
    if first == "8":
        return f"sh{symbol}"  # 688 科创板
    if first == "4":
        return f"bj{symbol}"  # 北交所
    return f"sz{symbol}"  # fallback


def _fetch_a_share(symbol: str, period: str) -> pd.DataFrame:
    """拉 A 股日线（使用新浪数据源，避免东方财富被防火墙拦截）"""
    try:
        import akshare as ak
        sina_symbol = _a_share_prefix(symbol)
        df = ak.stock_zh_a_daily(symbol=sina_symbol, adjust="qfq")
        if df is None or df.empty:
            log.warning(f"akshare A股 {sina_symbol} 返回空数据")
            return pd.DataFrame()
        # stock_zh_a_daily 返回列名是小写英文：date/open/high/low/close/volume
        df = _map_sina_columns(df)
        return _apply_period_filter(df, period)
    except Exception as e:
        log.error(f"akshare A股 {symbol} 拉取失败: {e}")
        return pd.DataFrame()


def _fetch_hk_stock(symbol: str, period: str) -> pd.DataFrame:
    """拉港股日线（使用新浪数据源）"""
    try:
        import akshare as ak
        df = ak.stock_hk_daily(symbol=symbol, adjust="qfq")
        if df is None or df.empty:
            log.warning(f"akshare 港股 {symbol} 返回空数据")
            return pd.DataFrame()
        df = _map_sina_columns(df)
        return _apply_period_filter(df, period)
    except Exception as e:
        log.error(f"akshare 港股 {symbol} 拉取失败: {e}")
        return pd.DataFrame()


# 已知指数代码 → 前缀映射（避免与 A 股代码冲突，如 000001 既是平安银行也是上证指数）
_KNOWN_INDICES = {
    "000001": "sh000001",   # 上证指数
    "399001": "sz399001",   # 深证成指
    "399006": "sz399006",   # 创业板指
    "000016": "sh000016",   # 上证50
    "000300": "sh000300",   # 沪深300
    "000688": "sh000688",   # 科创50
    "000905": "sh000905",   # 中证500
}


def _is_index(symbol: str) -> bool:
    """判断是否是指数代码"""
    return symbol in _KNOWN_INDICES


def _fetch_index(symbol: str, period: str) -> pd.DataFrame:
    """拉指数数据（使用新浪源 stock_zh_a_daily，指数不支持前复权）"""
    try:
        sina_symbol = _KNOWN_INDICES.get(symbol, f"sh{symbol}")
        import akshare as ak
        df = ak.stock_zh_a_daily(symbol=sina_symbol)  # 指数不需要 adjust 参数
        if df is None or df.empty:
            log.warning(f"akshare 指数 {sina_symbol} 返回空数据")
            return pd.DataFrame()
        df = _map_sina_columns(df)
        return _apply_period_filter(df, period)
    except Exception as e:
        log.error(f"akshare 指数 {symbol} 拉取失败: {e}")
        return pd.DataFrame()


# ==========================================
# 对外接口（与 exchange_fee.py 兼容）
# ==========================================


def get_history_data(
    symbol: str,
    period: str = "2y",
    as_of_date: Optional[str] = None,
) -> pd.DataFrame:
    """拉行情历史数据（替代 yfinance 的 get_history_data）

    Args:
        symbol: 股票代码
            - A股：6 位数字，如 '300476'（深市创业板）、'600900'（沪市主板）
            - 港股：5 位数字，如 '00700'（腾讯）
            - 指数：如 '000001'（上证指数）
        period: 时间窗口 '1mo'|'3mo'|'6mo'|'1y'|'2y'
        as_of_date: 回测用截止日期（ISO YYYY-MM-DD），正常调用不传

    Returns:
        pd.DataFrame with columns: Open, High, Low, Close, Volume, index=Date
        失败返回空 DataFrame
    """
    symbol = symbol.strip().upper()

    # 0. SQLite cache first. This prevents each monitor round from re-fetching
    # the same 2y daily bars from akshare for every symbol.
    cache_days = _period_to_days(period) + 10
    cached = _STORE.get_history_df(symbol, days=cache_days)
    cached = _apply_cutoff(_apply_period_filter(cached, period), as_of_date)
    if as_of_date and not cached.empty:
        return cached
    if _cache_is_fresh(cached):
        return cached

    # 路由优先级：指数 > A股 > 港股（指数代码可能与 A 股代码冲突，如 000001）
    if _is_index(symbol):
        df = _fetch_index(symbol, period)
    elif _is_a_share(symbol):
        df = _fetch_a_share(symbol, period)
    elif _is_hk_stock(symbol):
        df = _fetch_hk_stock(symbol, period)
    else:
        log.warning(f"akshare 无法识别 symbol: {symbol}，尝试作为 A 股代码")
        df = _fetch_a_share(symbol, period)

    if not df.empty:
        _save_to_cache(symbol, df, source="akshare")

    # as_of_date 过滤（回测穿越防护）
    return _apply_cutoff(df, as_of_date)


_MACRO_CACHE: dict = {}

def get_macro_snapshot() -> dict:
    """返回国内宏观指标快照（替代原有 VIX/TNX/USDCNY/AUDCNY）

    Returns:
        dict with keys:
          - cn_10y_bond: 中国10年期国债收益率
          - usdcny: 在岸人民币即期汇率
          - sh_index: 上证指数收盘价
          - sh_index_change_pct: 上证指数涨跌幅
          - north_flow: 北向资金最近交易日净流入（亿元）
          - as_of: 数据时间戳
    """
    # 5 分钟缓存：同一轮监控里 17 只股票不用每只都拉一遍国债+北向（耗时 16s×17）
    global _MACRO_CACHE
    now = datetime.now()
    if _MACRO_CACHE and (now - _MACRO_CACHE["_ts"]).total_seconds() < 300:
        return {k: v for k, v in _MACRO_CACHE.items() if k != "_ts"}
    out: dict = {"as_of": now.isoformat(timespec="seconds")}

    # 1. 中国10年期国债收益率
    try:
        import akshare as ak
        bond_df = ak.bond_china_yield()
        if bond_df is not None and not bond_df.empty:
            col_10y = None
            for c in bond_df.columns:
                if "10" in str(c):
                    col_10y = c
                    break
            if col_10y is None and len(bond_df.columns) > 1:
                col_10y = bond_df.columns[-2]  # 通常倒数第二列是 10 年期
            if col_10y:
                out["cn_10y_bond"] = round(float(bond_df[col_10y].iloc[-1]), 4)
            else:
                out["cn_10y_bond"] = None
        else:
            out["cn_10y_bond"] = None
    except Exception as e:
        log.warning(f"akshare 国债收益率拉取失败: {e}")
        out["cn_10y_bond"] = None

    # 2. 在岸人民币汇率
    try:
        import akshare as ak
        fx_df = ak.fx_spot_quote()
        if fx_df is not None and not fx_df.empty:
            # fx_spot_quote 返回多币种对人民币报价
            usd_row = fx_df[fx_df.iloc[:, 0].astype(str).str.contains("美元|USD", case=False, na=False)]
            if not usd_row.empty:
                out["usdcny"] = round(float(usd_row.iloc[0, -1]), 4)
            else:
                out["usdcny"] = None
        else:
            out["usdcny"] = None
    except Exception as e:
        log.warning(f"akshare 外汇汇率拉取失败: {e}")
        out["usdcny"] = None

    # 3. 上证指数
    try:
        sh_df = get_history_data("000001", "5d")
        if not sh_df.empty and len(sh_df) >= 2:
            out["sh_index"] = round(float(sh_df["Close"].iloc[-1]), 2)
            prev = float(sh_df["Close"].iloc[-2])
            cur = float(sh_df["Close"].iloc[-1])
            out["sh_index_change_pct"] = round((cur / prev - 1) * 100, 2)
        else:
            out["sh_index"] = None
            out["sh_index_change_pct"] = None
    except Exception as e:
        log.warning(f"akshare 上证指数拉取失败: {e}")
        out["sh_index"] = None
        out["sh_index_change_pct"] = None

    # 4. 北向资金净流入（沪股通(第0行)+深股通(第1行)净买额合计）
    try:
        import akshare as ak
        summary = ak.stock_hsgt_fund_flow_summary_em()
        if summary is not None and not summary.empty and len(summary) >= 2:
            col_net = 5  # 成交净买额 (net daily buy, 100M CNY)
            try:
                sh = float(summary.iloc[0, col_net])  # 沪股通
                sz = float(summary.iloc[1, col_net])  # 深股通
                if sh == sh and sz == sz:  # not NaN
                    out["north_flow"] = round(sh + sz, 2)
                elif sh == sh:
                    out["north_flow"] = round(sh, 2)
                elif sz == sz:
                    out["north_flow"] = round(sz, 2)
            except (ValueError, TypeError, IndexError):
                pass
    except Exception:
        # 北向资金接口可能不稳定，静默降级
        out["north_flow"] = None

    _MACRO_CACHE = {**out, "_ts": now}
    return out


def get_macro_data() -> str:
    """返回格式化的国内宏观指标文本（替代原有 VIX/TNX 报告）

    Returns:
        中文格式的宏观快照字符串
    """
    snap = get_macro_snapshot()

    lines = ["--- 国内宏观指标 ---"]
    if snap.get("cn_10y_bond") is not None:
        lines.append(f"1. 中国10年期国债收益率: {snap['cn_10y_bond']:.2f}%")
        lines.append("   *注意: 国债收益率上行→资金成本上升，压制股市估值；下行→利好成长股*")

    if snap.get("usdcny") is not None:
        lines.append(f"2. 在岸人民币 (USDCNY): {snap['usdcny']:.4f}")
        lines.append("   *注意: 人民币贬值→外资流出压力；升值→利好A股*")

    if snap.get("sh_index") is not None:
        change_str = f"({snap['sh_index_change_pct']:+.2f}%)" if snap.get("sh_index_change_pct") else ""
        lines.append(f"3. 上证指数: {snap['sh_index']:.0f} {change_str}")

    if snap.get("north_flow") is not None:
        direction = "净流入" if snap["north_flow"] > 0 else "净流出"
        lines.append(f"4. 北向资金: {direction} {abs(snap['north_flow']):.1f}亿元")
        lines.append("   *注意: 北向持续净流入→外资看多A股*")

    return "\n".join(lines)


def analyze_multi_timeframe(hist: pd.DataFrame, title: str) -> str:
    """多周期技术分析（直接复用 exchange_fee 的逻辑）

    本函数作为桥接：akshare_data.get_history_data() → exchange_fee.analyze_multi_timeframe()
    """
    from utils.exchange_fee import analyze_multi_timeframe as _orig
    return _orig(hist, title)


__all__ = [
    "get_history_data",
    "get_macro_snapshot",
    "get_macro_data",
    "analyze_multi_timeframe",
]
