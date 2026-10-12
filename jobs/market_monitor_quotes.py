"""Market data and committee-call helpers for intraday monitoring."""
from __future__ import annotations

import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo
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
                   behavioral_factor: Optional[Dict[str, Any]] = None,
                   hk_spatio_factor: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
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
        hk_spatio_factor=hk_spatio_factor or {},
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


BEHAVIORAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
BEHAVIORAL_REBALANCE_SESSIONS = 5


def _parse_factor_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEHAVIORAL_TIMEZONE)
    return parsed.astimezone(BEHAVIORAL_TIMEZONE)


def _next_behavioral_session_open(moment: datetime) -> str:
    """Return the next XSHG session open after a factor calculation."""
    import exchange_calendars as xcal

    local_moment = moment.astimezone(BEHAVIORAL_TIMEZONE)
    calendar = xcal.get_calendar("XSHG")
    session = calendar.date_to_session(local_moment.date().isoformat(), direction="next")
    session_date = session.date()
    if session_date <= local_moment.date() and local_moment.time() >= clock_time(9, 30):
        session = calendar.next_session(session)
    return datetime.combine(
        session.date(),
        clock_time(9, 30),
        tzinfo=BEHAVIORAL_TIMEZONE,
    ).isoformat(timespec="minutes")


def _factor_targets(assessments: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(symbol): {
            "selected": bool(getattr(assessment, "selected", False))
            if not isinstance(assessment, dict)
            else bool(assessment.get("selected", False)),
            "target_weight_pct": float(
                getattr(assessment, "target_weight_pct", 0.0)
                if not isinstance(assessment, dict)
                else assessment.get("target_weight_pct", 0.0)
                or 0.0
            ),
        }
        for symbol, assessment in assessments.items()
    }


def _read_behavioral_state(state_file: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _activate_due_behavioral_targets(
    state_file: Path,
    state: Dict[str, Any],
    *,
    now: datetime,
) -> Dict[str, Any]:
    pending = state.get("pending_target_update")
    if not isinstance(pending, dict):
        return state
    effective_at = _parse_factor_timestamp(pending.get("targets_effective_at"))
    if effective_at is None or now.astimezone(BEHAVIORAL_TIMEZONE) < effective_at:
        return state

    from utils.safe_persistence import atomic_write_json

    activated = {
        **state,
        "rebalance_date": pending.get("rebalance_date", state.get("rebalance_date", "")),
        "score_updated_at": pending.get("score_updated_at", state.get("score_updated_at", "")),
        "targets_effective_at": pending.get("targets_effective_at", ""),
        "targets": pending.get("targets", {}),
        "activation_policy": "next_trading_session",
    }
    activated.pop("pending_target_update", None)
    try:
        atomic_write_json(state_file, activated)
    except Exception as exc:
        log.warning(f"A股行为因子待生效目标激活状态写入失败: {exc}")
    return activated


def schedule_behavioral_factor_targets(
    assessments: Dict[str, Any],
    *,
    latest_date: str,
    score_updated_at: Optional[str] = None,
    state_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Persist Sunday scores for activation at the next trading-session open."""
    from jobs.market_monitor_common import _PROJECT_ROOT
    from utils.safe_persistence import atomic_write_json

    state_file = state_path or (_PROJECT_ROOT / "data" / "behavioral_factor_state.json")
    previous = _read_behavioral_state(state_file)
    calculation_time = (now or datetime.now(BEHAVIORAL_TIMEZONE)).astimezone(
        BEHAVIORAL_TIMEZONE
    )
    score_time = score_updated_at or calculation_time.isoformat(timespec="seconds")
    effective_at = _next_behavioral_session_open(calculation_time)
    targets = _factor_targets(assessments)
    pending = {
        "rebalance_date": str(latest_date or ""),
        "score_updated_at": score_time,
        "targets_effective_at": effective_at,
        "targets": targets,
    }
    state = {
        "model": "a_share_behavioral_v1",
        "selection_scope": "factor_model_target_portfolio",
        "represents_account_holdings": False,
        "selection_semantics": (
            "selected means membership in the factor model target portfolio; "
            "it never means a real or committee account holding"
        ),
        "rebalance_sessions": BEHAVIORAL_REBALANCE_SESSIONS,
        "activation_policy": "next_trading_session",
        "rebalance_date": str(previous.get("rebalance_date") or ""),
        "score_updated_at": str(previous.get("score_updated_at") or ""),
        "targets_effective_at": str(previous.get("targets_effective_at") or ""),
        "targets": dict(previous.get("targets") or {}),
        "pending_target_update": pending,
    }
    if not state["targets"] and _parse_factor_timestamp(effective_at) <= calculation_time:
        state.update(pending)
        state.pop("pending_target_update", None)
    atomic_write_json(state_file, state)
    return state


def build_behavioral_factor_context(
    stocks: List[Dict[str, Any]],
    *,
    state_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build a shared cross-section using the currently active target state."""
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
    previous = _read_behavioral_state(state_file)
    calculation_time = (now or datetime.now(BEHAVIORAL_TIMEZONE)).astimezone(
        BEHAVIORAL_TIMEZONE
    )
    previous = _activate_due_behavioral_targets(
        state_file,
        previous,
        now=calculation_time,
    )

    longest = max(histories.values(), key=len)
    calendar = sorted(set(str(value)[:10] for value in longest.index))
    latest_date = calendar[-1] if calendar else ""
    previous_date = str(previous.get("rebalance_date") or "")
    sessions_since = sum(1 for value in calendar if previous_date and value > previous_date)
    previous_targets = dict(previous.get("targets") or {})
    scheduled_policy = previous.get("activation_policy") == "next_trading_session"
    refresh_targets = (
        not scheduled_policy
        and (not previous_targets or not previous_date or sessions_since >= BEHAVIORAL_REBALANCE_SESSIONS)
    )
    state_semantics = {
        "selection_scope": "factor_model_target_portfolio",
        "represents_account_holdings": False,
        "selection_semantics": (
            "selected means membership in the factor model target portfolio; "
            "it never means a real or committee account holding"
        ),
    }

    if refresh_targets:
        target_refresh_at = datetime.now(
            ZoneInfo("Asia/Shanghai")
        ).isoformat(timespec="seconds")
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
                "rebalance_sessions": BEHAVIORAL_REBALANCE_SESSIONS,
                "score_updated_at": target_refresh_at,
                "targets_effective_at": target_refresh_at,
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


def _last_completed_hk_date() -> str:
    now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    candidate = now.date()
    if now.time() < clock_time(16, 15):
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate.isoformat()


def build_hk_spatio_factor_context(
    stocks: List[Dict[str, Any]],
    *,
    state_path: Optional[Path] = None,
    reference_symbols: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build the HK cross-section with targets frozen for twenty sessions."""
    from core.hk_spatio_temporal_factor import (
        DEFAULT_HK_REFERENCE_UNIVERSE,
        HKSpatioTemporalAssessment,
        MODEL_KEY,
        REFERENCE_UNIVERSE_KEY,
        assess_hk_spatio_universe,
        normalize_hk_symbol,
    )

    requested_symbols = set()
    for stock in stocks:
        raw_symbol = str(stock.get("symbol") or "").strip().upper()
        market = str(stock.get("market") or "").strip().lower()
        is_hk = market in {"hk", "h", "hongkong", "hong_kong", "港股"} or raw_symbol.endswith(".HK")
        symbol = normalize_hk_symbol(raw_symbol)
        if is_hk and symbol:
            requested_symbols.add(symbol)
    if not requested_symbols:
        return {}
    effective_reference = (
        DEFAULT_HK_REFERENCE_UNIVERSE
        if reference_symbols is None
        else tuple(reference_symbols)
    )
    reference_universe_key = (
        REFERENCE_UNIVERSE_KEY if reference_symbols is None else "caller_supplied_reference"
    )
    symbols = requested_symbols | {
        normalized
        for value in effective_reference
        if (normalized := normalize_hk_symbol(value))
    }
    ordered_symbols = sorted(symbols)

    from jobs.market_monitor_common import _PROJECT_ROOT
    from utils.market_data_provider import get_history_data
    from utils.safe_persistence import atomic_write_json

    histories: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(ordered_symbols))) as pool:
        futures = {
            pool.submit(get_history_data, symbol, "2y"): symbol
            for symbol in ordered_symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                frame = future.result()
                if frame is not None and not frame.empty:
                    histories[symbol] = frame
            except Exception as exc:
                log.warning(f"港股时空动量历史数据缺失 {symbol}: {exc}")

    completed_date = _last_completed_hk_date()
    assessments = assess_hk_spatio_universe(histories, as_of=completed_date)
    for symbol in requested_symbols:
        assessments.setdefault(
            symbol,
            HKSpatioTemporalAssessment(
                symbol=symbol,
                reason="requested_symbol_history_unavailable",
            ),
        )
    if not assessments:
        return {}
    if not any(not assessment.low_confidence for assessment in assessments.values()):
        return {
            symbol: assessments[symbol].as_dict()
            for symbol in requested_symbols
        }

    state_file = state_path or (_PROJECT_ROOT / "data" / "hk_spatio_factor_state.json")
    previous: Dict[str, Any] = {}
    try:
        if state_file.exists():
            previous = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning(f"港股时空动量调仓状态不可读，将重建: {exc}")

    calendar = sorted({
        str(value)[:10]
        for frame in histories.values()
        for value in frame.index
        if str(value)[:10] <= completed_date
    })
    latest_date = calendar[-1] if calendar else ""
    previous_date = str(previous.get("rebalance_date") or "")
    sessions_since = sum(1 for value in calendar if previous_date and value > previous_date)
    previous_targets = dict(previous.get("targets") or {})
    refresh_targets = (
        str(previous.get("model") or "") != MODEL_KEY
        or str(previous.get("reference_universe") or "") != reference_universe_key
        or not previous_targets
        or not previous_date
        or sessions_since >= 20
    )
    state_semantics = {
        "selection_scope": "hk_factor_model_target_portfolio",
        "represents_account_holdings": False,
        "selection_semantics": (
            "selected means membership in the Hong Kong factor target portfolio; "
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
                "model": MODEL_KEY,
                **state_semantics,
                "rebalance_date": latest_date,
                "rebalance_sessions": 20,
                "completed_bars_through": completed_date,
                "reference_universe": reference_universe_key,
                "reference_symbol_count": len(effective_reference),
                "targets": targets,
            })
        except Exception as exc:
            log.warning(f"港股时空动量调仓状态写入失败，本轮结果仍可使用: {exc}")
    else:
        targets = previous_targets
        if any(previous.get(key) != value for key, value in state_semantics.items()):
            try:
                atomic_write_json(state_file, {**previous, **state_semantics})
            except Exception as exc:
                log.warning(f"港股时空动量调仓状态语义迁移失败，本轮结果仍可使用: {exc}")
        assessments = {
            symbol: replace(
                assessment,
                selected=bool((targets.get(symbol) or {}).get("selected", False)),
                target_weight_pct=float(
                    (targets.get(symbol) or {}).get("target_weight_pct", 0.0) or 0.0,
                ),
                reason=(
                    assessment.reason
                    + f";target_membership_frozen_from_{previous_date}_{sessions_since}_of_20_sessions"
                ),
            )
            for symbol, assessment in assessments.items()
        }
    return {
        symbol: assessments[symbol].as_dict()
        for symbol in requested_symbols
    }


# ==========================================
# 通知
# ==========================================
