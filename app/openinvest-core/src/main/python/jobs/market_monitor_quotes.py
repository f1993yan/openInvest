"""Market data and committee-call helpers for intraday monitoring."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from jobs.market_monitor_common import log
from utils.market_data_provider import fetch_prices as fetch_sina_prices
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
                   optimizer_review_enabled: bool = True,
                   shadow_position_pct: float = 0.0,
                   shadow_cost: float = 0.0,
                   shadow_cash: float = 0.0,
                   shadow_holdings: Optional[List[Dict]] = None,
                   shadow_available_cash: float = 0.0,
                   shadow_t2_pending: float = 0.0) -> Optional[Dict]:
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
        shadow_position_pct=shadow_position_pct,
        shadow_cost=shadow_cost,
        shadow_cash=shadow_cash,
        shadow_holdings=[BackendHolding(
            symbol=h["symbol"],
            name=h.get("name", ""),
            weight_pct=h.get("weight_pct", h.get("position_pct", 0)),
            cost=h.get("cost", 0),
            current_price=h.get("current_price"),
        ) for h in (shadow_holdings or [])],
        shadow_available_cash=shadow_available_cash,
        shadow_t2_pending_cash=shadow_t2_pending,
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
