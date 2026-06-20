"""Unified market data provider layer to isolate external data sources."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
import pandas as pd
import requests

log = logging.getLogger(__name__)

def fetch_prices(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """获取标的最新价格行情。
    
    返回格式:
        {symbol: {"price": float, "prev_close": float, "change_pct": float, "name": str}}
    """
    if not symbols:
        return {}

    # 自动补充新浪前缀（sz/sh/hk）
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
            log.warning(f"新浪行情第{attempt+1}次返回空数据，重试...")
            time.sleep(2)
        except Exception as e:
            log.warning(f"新浪行情第{attempt+1}次失败: {e}")
            time.sleep(2)
            
    if not text or 'hq_str_' not in text:
        log.error(f"新浪行情3次重试均失败")
        return {}

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

            results[symbol] = {
                "name": name,
                "price": price,
                "prev_close": prev_close,
                "change_pct": round(change_pct, 2),
            }
        except Exception as e:
            log.warning(f"解析行情行失败: {line[:60]}... {e}")

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
    
    # 判定是否为 A 股代码
    is_a_share = False
    if symbol_clean.isdigit() and len(symbol_clean) == 6:
        is_a_share = True
    elif (symbol_clean.startswith("SH") or symbol_clean.startswith("SZ")) and symbol_clean[2:].isdigit() and len(symbol_clean[2:]) == 6:
        is_a_share = True
        
    if is_a_share:
        try:
            from utils.akshare_data import get_history_data as _ak_get_history_data
            df = _ak_get_history_data(symbol, period)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            log.warning(f"akshare 获取 A 股历史行情失败 for {symbol}: {e}")
            
    # 全球/美股/外汇等走 exchange_fee (yfinance) 数据源
    try:
        from utils.exchange_fee import get_history_data as _fallback_get_history_data
        return _fallback_get_history_data(symbol, period, as_of_date=as_of_date)
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
