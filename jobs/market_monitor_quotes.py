"""Market data and committee-call helpers for intraday monitoring."""
from __future__ import annotations

import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
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
                   shadow_t2_pending: float = 0.0,
                   behavioral_factor: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
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
        behavioral_factor=behavioral_factor or {},
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


def build_behavioral_factor_context(
    stocks: List[Dict[str, Any]],
    *,
    state_path: Optional[Path] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build a shared cross-section with a persistent five-session target."""
    symbols = sorted({
        str(stock.get("symbol") or "").strip().upper()
        for stock in stocks
        if str(stock.get("market") or "a").lower() == "a"
        and len(str(stock.get("symbol") or "").strip()) == 6
    })
    if not symbols:
        return {}
    from core.ashare_behavioral_factor import assess_behavioral_universe
    from jobs.market_monitor_common import _PROJECT_ROOT
    from utils.market_data_provider import get_history_data
    from utils.safe_persistence import atomic_write_json

    histories: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        futures = {pool.submit(get_history_data, symbol, "2y"): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                frame = future.result()
                if frame is not None and not frame.empty:
                    histories[symbol] = frame
            except Exception as exc:
                log.warning(f"A股行为因子历史数据缺失 {symbol}: {exc}")
    assessments = assess_behavioral_universe(histories)
    if not assessments:
        return {}

    state_file = state_path or (_PROJECT_ROOT / "data" / "behavioral_factor_state.json")
    previous: Dict[str, Any] = {}
    try:
        if state_file.exists():
            previous = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning(f"A股行为因子调仓状态不可读，将重建: {exc}")

    longest = max(histories.values(), key=len)
    calendar = sorted(set(str(value)[:10] for value in longest.index))
    latest_date = calendar[-1] if calendar else ""
    previous_date = str(previous.get("rebalance_date") or "")
    sessions_since = sum(1 for value in calendar if previous_date and value > previous_date)
    previous_targets = dict(previous.get("targets") or {})
    refresh_targets = not previous_targets or not previous_date or sessions_since >= 5
    state_semantics = {
        "selection_scope": "factor_model_target_portfolio",
        "represents_account_holdings": False,
        "selection_semantics": (
            "selected means membership in the factor model target portfolio; "
            "it never means a real or committee account holding"
        ),
    }

    if refresh_targets:
        targets = {
            symbol: {
                "selected": assessment.selected,
                "target_weight_pct": assessment.target_weight_pct,
            }
            for symbol, assessment in assessments.items()
        }
        try:
            atomic_write_json(state_file, {
                "model": "a_share_behavioral_v1",
                **state_semantics,
                "rebalance_date": latest_date,
                "rebalance_sessions": 5,
                "targets": targets,
            })
        except Exception as exc:
            log.warning(f"A股行为因子调仓状态写入失败，本轮结果仍可使用: {exc}")
    else:
        targets = previous_targets
        if any(previous.get(key) != value for key, value in state_semantics.items()):
            try:
                atomic_write_json(state_file, {**previous, **state_semantics})
            except Exception as exc:
                log.warning(f"A股行为因子调仓状态语义迁移失败，本轮结果仍可使用: {exc}")
        assessments = {
            symbol: replace(
                assessment,
                selected=bool((targets.get(symbol) or {}).get("selected", False)),
                target_weight_pct=float((targets.get(symbol) or {}).get("target_weight_pct", 0.0) or 0.0),
                reason=(
                    assessment.reason
                    + f";target_membership_frozen_from_{previous_date}_{sessions_since}_of_5_sessions"
                ),
            )
            for symbol, assessment in assessments.items()
        }
    return {symbol: assessment.as_dict() for symbol, assessment in assessments.items()}


# ==========================================
# 通知
# ==========================================
