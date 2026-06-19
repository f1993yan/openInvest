"""Market data and committee-call helpers for intraday monitoring."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from jobs.market_monitor_common import log

def fetch_sina_prices(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """从新浪 API 拉取最新行情

    Returns:
        {symbol: {"price": float, "prev_close": float, "change_pct": float, "name": str}}
    """
    # 自动补充新浪前缀（sz/sh/hk），避免在公开源码里维护个人关注池映射。
    def _to_sina(sym):
        if sym.isdigit() and len(sym) == 6:
            return f"sh{sym}" if sym[0] == "6" else f"sz{sym}"
        if sym.isdigit() and len(sym) == 5:
            return f"hk{sym}"
        return sym

    sina_list = ",".join(_to_sina(s) for s in symbols)
    url = f"https://hq.sinajs.cn/list={sina_list}"

    # 重试3次，应对新浪API偶发抖动
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
            # 从 var_name 提取 symbol
            # 格式: var hq_str_sh600900="..."
            symbol_part = var_name.replace("var hq_str_", "").strip()
            # symbol_part like "sh600900" → "600900"
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
            # 港股格式不同
            if "hk" in var_name.lower():
                # hk: name, english_name, open, prev_close, price, high, low, ...
                if len(fields) >= 10:
                    prev_close = float(fields[3]) if fields[3] else 0
                    price = float(fields[5]) if fields[5] else 0
                    change_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0
                else:
                    continue
            else:
                # A股: name, open, prev_close, price, high, low, ...
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


# ==========================================
# 委员会 API 调用
# ==========================================

def call_committee(symbol: str, name: str, market: str,
                   position_pct: float, cost: float, current_price: float,
                   total_assets: float, cash: float,
                   all_holdings: List[Dict],
                   target_position_pct: Optional[float] = None,
                   t2_pending: float = 0.0,
                   news_brief: str = "",
                   sector: str = "",
                   industry: str = "",
                   fundamentals: Optional[Dict[str, Any]] = None,
                   optimizer_review_enabled: bool = True) -> Optional[Dict]:
    """调用委员会分析（直接 Python 调用，不需要后端端口）"""
    from backend.server import run_committee_direct, CommitteeRequest, Holding as BackendHolding
    available_cash = max(float(cash or 0), 0.0)

    req = CommitteeRequest(
        symbol=symbol,
        name=name,
        market=market,
        sector=sector,
        industry=industry,
        position_pct=position_pct,
        target_position_pct=target_position_pct,
        cost=cost,
        current_price=current_price,
        total_assets=total_assets,
        cash=available_cash,
        holdings=[BackendHolding(
            symbol=h["symbol"],
            name=h.get("name", ""),
            weight_pct=h.get("weight_pct", h.get("position_pct", 0)),
            cost=h.get("cost", 0),
            current_price=h.get("current_price"),
        ) for h in all_holdings],
        min_lot_size=100 if market == "a" else 100,
        t_plus_1=market == "a",
        available_cash=available_cash,
        t2_pending_cash=t2_pending,
        news_brief=news_brief,
        fundamentals=fundamentals or {},
        optimizer_review_enabled=optimizer_review_enabled,
        max_debate_rounds=2,  # 监控模式减半辩论轮数，加速
    )
    try:
        response = run_committee_direct(req)
        return response.model_dump()
    except Exception as e:
        log.error(f"委员会 {symbol} 失败: {e}")
        return {"success": False, "symbol": symbol, "error": str(e)}


# ==========================================
# 通知
# ==========================================
