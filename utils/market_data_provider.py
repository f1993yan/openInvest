"""Unified market data provider layer to isolate external data sources."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
import pandas as pd
import requests

log = logging.getLogger(__name__)

_PRICE_CACHE: Dict[str, Dict[str, Any]] = {}

def is_trading_time() -> bool:
    """判断当前是否在交易时段内（交易日 9:30-15:00）"""
    try:
        from utils.market_calendar import is_trading_day
        from datetime import datetime, time as dt_time
        now = datetime.now()
        if not is_trading_day("XSHG", now.date()):
            return False
        t = now.time()
        return dt_time(9, 30) <= t <= dt_time(15, 0)
    except Exception:
        from datetime import datetime, time as dt_time
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.time()
        return dt_time(9, 30) <= t <= dt_time(15, 0)

def fetch_prices(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """获取标的最新价格行情。
    
    返回格式:
        {symbol: {"price": float, "prev_close": float, "change_pct": float, "name": str}}
    """
    if not symbols:
        return {}

    # 如果不在交易时间，尝试完全使用缓存
    if not is_trading_time():
        cached_results = {}
        all_cached = True
        for s in symbols:
            s_upper = s.upper()
            if s_upper in _PRICE_CACHE:
                cached_results[s] = _PRICE_CACHE[s_upper]
            else:
                all_cached = False
                break
        if all_cached:
            return cached_results

    # 腾讯 qt.gtimg.cn 批量报价（A股/港股通用）
    def _to_tx(sym):
        s = sym.strip().upper()
        if s.isdigit() and len(s) == 6:
            return f"sh{s}" if s[0] in ("5", "6") else f"sz{s}"
        if s.isdigit() and len(s) == 5:
            return f"hk{s}"
        return sym

    tx_list = ",".join(_to_tx(s) for s in symbols)
    url = f"https://qt.gtimg.cn/q={tx_list}"

    text = ""
    for attempt in range(3):
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            resp.encoding = "gbk"
            text = resp.text
            if text and '="' in text:
                break
            log.warning(f"腾讯行情第{attempt+1}次返回空数据，重试...")
            time.sleep(2)
        except Exception as e:
            log.warning(f"腾讯行情第{attempt+1}次失败: {e}")
            time.sleep(2)

    if not text or '="' not in text:
        log.error(f"腾讯行情3次重试均失败，降级到 Sina")
        return _fetch_prices_sina_fallback(symbols)
        # 降级：如果获取失败，尽最大努力返回能拿到的缓存
        fallback_results = {}
        for s in symbols:
            s_upper = s.upper()
            if s_upper in _PRICE_CACHE:
                fallback_results[s] = _PRICE_CACHE[s_upper]
        return fallback_results

    results = {}
    for line in text.strip().split("\n"):
        if not line.strip() or "=" not in line:
            continue
        try:
            var_name, data = line.split("=", 1)
            # 腾讯格式: v_sh600519="1~茅台~600519~...~1850.00~..."
            # 提取 prefix+code
            symbol_part = var_name.replace("v_", "").strip()
            if symbol_part.startswith("sh"):
                symbol = symbol_part[2:]
            elif symbol_part.startswith("sz"):
                symbol = symbol_part[2:]
            elif symbol_part.startswith("hk"):
                symbol = symbol_part[2:]
            else:
                symbol = symbol_part

            data = data.strip('";\n ')
            fields = data.split("~")

            if len(fields) < 6:
                continue

            name = fields[1] if len(fields) > 1 else symbol
            try:
                price = float(fields[3]) if fields[3] else 0
                prev_close = float(fields[4]) if fields[4] else 0
            except (ValueError, IndexError):
                continue

            change_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0

            item = {
                "name": name,
                "price": price,
                "prev_close": prev_close,
                "change_pct": round(change_pct, 2),
            }
            results[symbol] = item
            _PRICE_CACHE[symbol.upper()] = item
        except Exception as e:
            log.warning(f"解析腾讯行情行失败: {line[:60]}... {e}")

    # 没拿到的用旧缓存补齐
    for s in symbols:
        if s not in results:
            s_upper = s.upper()
            if s_upper in _PRICE_CACHE:
                results[s] = _PRICE_CACHE[s_upper]

    return results


def _fetch_prices_sina_fallback(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Sina hq.sinajs.cn 降级路径，保留原实现。"""
    def _to_sina(sym):
        if sym.isdigit() and len(sym) == 6:
            return f"sh{sym}" if sym[0] == "6" else f"sz{sym}"
        if sym.isdigit() and len(sym) == 5:
            return f"hk{sym}"
        return sym

    sina_list = ",".join(_to_sina(s) for s in symbols)
    url = f"https://hq.sinajs.cn/list={sina_list}"

    text = ""
    for attempt in range(3):
        try:
            resp = requests.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=15)
            resp.encoding = "gbk"
            text = resp.text
            if text and 'hq_str_' in text:
                break
            log.warning(f"Sina降级第{attempt+1}次返回空数据，重试...")
            time.sleep(2)
        except Exception as e:
            log.warning(f"Sina降级第{attempt+1}次失败: {e}")
            time.sleep(2)

    if not text or 'hq_str_' not in text:
        log.error(f"Sina降级3次重试均失败")
        fallback_results = {}
        for s in symbols:
            s_upper = s.upper()
            if s_upper in _PRICE_CACHE:
                fallback_results[s] = _PRICE_CACHE[s_upper]
        return fallback_results

    results = {}
    for line in text.strip().split("\n"):
        if not line.strip() or "=" not in line:
            continue
        try:
            var_name, data = line.split("=", 1)
            symbol_part = var_name.replace("var hq_str_", "").strip()
            if symbol_part.startswith("sh"):
                symbol = symbol_part[2:]
            elif symbol_part.startswith("sz"):
                symbol = symbol_part[2:]
            elif symbol_part.startswith("hk"):
                symbol = symbol_part[2:]
            else:
                symbol = symbol_part

            data = data.strip('";\n ')
            fields = data.split(",")

            if len(fields) < 4:
                continue

            name = fields[0]
            if "hk" in var_name.lower():
                if len(fields) >= 10:
                    prev_close = float(fields[3]) if fields[3] else 0
                    price = float(fields[5]) if fields[5] else 0
                    change_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0
                else:
                    continue
            else:
                if len(fields) >= 4:
                    prev_close = float(fields[2]) if fields[2] else 0
                    price = float(fields[3]) if fields[3] else 0
                    change_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0
                else:
                    continue

            item = {
                "name": name,
                "price": price,
                "prev_close": prev_close,
                "change_pct": round(change_pct, 2),
            }
            results[symbol] = item
            _PRICE_CACHE[symbol.upper()] = item
        except Exception as e:
            log.warning(f"Sina降级解析失败: {line[:60]}... {e}")

    for s in symbols:
        if s not in results:
            s_upper = s.upper()
            if s_upper in _PRICE_CACHE:
                results[s] = _PRICE_CACHE[s_upper]

    return results

def get_history_data(
    symbol: str,
    period: str = "1y",
    as_of_date: Optional[str] = None,
) -> pd.DataFrame:
    """获取标的历史日线行情 DataFrame。
    
    自动识别 CN/US/HK 等标的并走对应的数据源接入。
    """
    symbol_clean = symbol.strip().upper()
    akshare_symbol: Optional[str] = None
    asset_label = "标的"

    # 判定是否为 A 股 / 港股代码，并归一化为 akshare_data 可识别的裸码。
    if symbol_clean.isdigit() and len(symbol_clean) == 6:
        akshare_symbol = symbol_clean
        asset_label = "A股"
    elif symbol_clean.startswith(("SH", "SZ")) and symbol_clean[2:].isdigit() and len(symbol_clean[2:]) == 6:
        akshare_symbol = symbol_clean[2:]
        asset_label = "A股"
    elif symbol_clean.endswith((".SS", ".SH", ".SZ")) and symbol_clean[:-3].isdigit() and len(symbol_clean[:-3]) == 6:
        akshare_symbol = symbol_clean[:-3]
        asset_label = "A股"
    elif symbol_clean.endswith(".HK") and symbol_clean[:-3].isdigit() and 1 <= len(symbol_clean[:-3]) <= 5:
        akshare_symbol = symbol_clean[:-3].zfill(5)
        asset_label = "港股"
    elif symbol_clean.isdigit() and 1 <= len(symbol_clean) <= 5:
        akshare_symbol = symbol_clean.zfill(5)
        asset_label = "港股"

    if akshare_symbol is not None:
        try:
            from utils.akshare_data import get_history_data as _ak_get_history_data
            df = _ak_get_history_data(akshare_symbol, period, as_of_date=as_of_date)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            log.warning(f"akshare 获取{asset_label}历史行情失败 for {symbol}: {e}")
            
    # 全球/美股/外汇等走 exchange_fee (yfinance) 数据源
    try:
        from utils.exchange_fee import get_history_data as _fallback_get_history_data
        fallback_symbol = symbol_clean
        if akshare_symbol is not None and asset_label == "港股":
            fallback_symbol = f"{akshare_symbol}.HK"
        return _fallback_get_history_data(fallback_symbol, period, as_of_date=as_of_date)
    except Exception as e:
        log.warning(f"exchange_fee 获取历史行情失败 for {symbol}: {e}")
        return pd.DataFrame()


def search_symbols(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """搜索标的代码与简称。
    
    返回格式:
        [{"symbol": str, "name": str, "market": str}]
    """
    query_clean = query.strip().lower()
    if not query_clean:
        return []
        
    results = []
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        for _, row in df.iterrows():
            code = str(row.get("code") or row.get("证券代码") or "").strip()
            name = str(row.get("name") or row.get("证券简称") or "").strip()
            if not code or not name:
                continue
            if query_clean in code.lower() or query_clean in name.lower():
                results.append({
                    "symbol": code,
                    "name": name,
                    "market": "a",
                })
            if len(results) >= limit:
                break
    except Exception as e:
        log.warning(f"A股标的搜寻失败 '{query}': {e}")
        
    return results


def get_macro_data() -> str:
    """获取宏观数据报告。"""
    try:
        from utils.akshare_data import get_macro_data as _ak_get_macro_data
        return _ak_get_macro_data()
    except Exception as e:
        log.warning(f"Failed to get macro data: {e}")
        return ""


def get_macro_snapshot(as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """获取宏观快照数据。"""
    try:
        from utils.akshare_data import get_macro_snapshot as _ak_get_macro_snapshot
        return _ak_get_macro_snapshot(as_of_date=as_of_date)
    except Exception as e:
        log.warning(f"Failed to get macro snapshot: {e}")
        return {}
