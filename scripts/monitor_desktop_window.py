"""Standalone desktop monitor window for intraday target status.

The market monitor writes ``data/market_monitor/latest_window.json`` after each
round. This Tkinter window reads that snapshot, refreshes in place, and shakes
when a new action-required state appears. Clicking a row opens a dialog and runs
the latest committee analysis for that symbol in a background thread.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "data" / "market_monitor" / "latest_window.json"
WEEKEND_NEWS_DIR = ROOT / "data" / "weekend_news"
DAILY_SELECTION_LATEST = ROOT / "data" / "daily_stock_selection" / "latest.json"
BACKGROUND_PROCESS_PATTERNS = (
    "jobs.market_monitor",
    "scheduler.runner",
    "jobs.weekend_news_crawl",
    "jobs.daily_stock_selection",
    "scripts.daily_stock_selection",
)
BOARD_BG = "#f3f6fb"
PANEL_BG = "#ffffff"
HEADER_BG = "#2f80ed"
HEADER_FG = "#ffffff"
TEXT = "#18202b"
MUTED = "#667085"
LINE = "#e6ebf2"
CARD_BG = "#ffffff"
CARD_ACTIVE_BG = "#eaf2ff"
BLUE = "#2f80ed"
SOFT_BLUE = "#e8f1ff"
ACTION_BG = "#fff3f0"
ACTION_FG = "#c01048"
TRIGGER_BG = "#fff8e8"
TRIGGER_FG = "#b54708"
UP_FG = "#067647"
DOWN_FG = "#b42318"
STOCK_CARD_STAGE_HEIGHT = 144
NEWS_CARD_STAGE_HEIGHT = 250

STATE_LABELS = {
    "action_required": "需要操作",
    "trigger_confirmed": "触发确认",
    "watch_trigger": "单轮触发",
    "candidate": "候选",
    "monitoring": "监控中",
    "blocked": "已拦截",
    "error": "错误",
}

STATE_SIGNALS = {
    "action_required": "操作",
    "trigger_confirmed": "触发",
    "watch_trigger": "预警",
    "candidate": "候选",
    "monitoring": "观察",
    "blocked": "拦截",
    "error": "错误",
}

VERDICT_ARROWS = {
    "BUY": "↑",
    "ACCUMULATE": "↗",
    "HOLD": "→",
    "WAIT": "→",
    "TRIM": "↘",
    "SELL": "↓",
}

STATE_PRIORITY = {
    "action_required": 0,
    "trigger_confirmed": 1,
    "watch_trigger": 2,
    "candidate": 3,
    "monitoring": 4,
    "blocked": 5,
    "error": 6,
}

VERDICT_PRIORITY = {
    "SELL": 0,
    "TRIM": 1,
    "BUY": 2,
    "ACCUMULATE": 3,
    "HOLD": 4,
    "WAIT": 5,
}


def _safe_num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _stop_background_services() -> None:
    """Stop OpenInvest background workers started for the desktop window."""
    if os.name != "nt":
        return
    root = str(ROOT).replace("'", "''").lower()
    patterns = ",".join(f"'{pattern}'" for pattern in BACKGROUND_PROCESS_PATTERNS)
    script = f"""
$root = '{root}'
$patterns = @({patterns})
$all = @(Get-CimInstance Win32_Process)
$targets = @($all | Where-Object {{
  $cmd = ($_.CommandLine + '').ToLowerInvariant()
  $exe = ($_.ExecutablePath + '').ToLowerInvariant()
  $name = ($_.Name + '').ToLowerInvariant()
  ($name -match '^(cmd|uv|uvicorn|python|pythonw)\\.exe$') -and
  ($cmd.Contains($root) -or $exe.Contains($root)) -and
  ($patterns | Where-Object {{ $cmd.Contains($_) }})
}})
foreach ($target in $targets) {{
  Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
}}
foreach ($target in $targets) {{
  Wait-Process -Id $target.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
}}
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _fmt_price(value: Any) -> str:
    value = _safe_num(value)
    return f"{value:.2f}" if value > 0 else "-"


def _fmt_money(value: Any) -> str:
    value = _safe_num(value)
    return f"{value:,.0f}" if abs(value) >= 1 else "-"


def _fmt_cash_line(cash: Any, total_assets: Any) -> str:
    cash_value = _safe_num(cash)
    total_value = _safe_num(total_assets)
    pct = cash_value / total_value * 100.0 if total_value > 0 else 0.0
    return f"现金 {_fmt_money(cash_value)} / {pct:.1f}%"


def _cash_ratio(cash: Any, total_assets: Any) -> float:
    cash_value = max(_safe_num(cash), 0.0)
    total_value = max(_safe_num(total_assets), 0.0)
    if total_value <= 0:
        return 0.0
    return max(0.0, min(cash_value / total_value, 1.0))


def _fmt_update_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "更新 -"
    try:
        dt = datetime.fromisoformat(text)
        return f"更新 {dt.strftime('%H:%M:%S')}"
    except Exception:
        return f"更新 {text}"


def _fmt_lots(value: float) -> str:
    if abs(value) >= 1:
        return f"{math.floor(abs(value))}手"
    return "-"


def _fmt_holding_lots(row: Dict[str, Any]) -> str:
    units = _safe_num(row.get("units"))
    lot_size = _lot_size(row)
    if units <= 0 or lot_size <= 0:
        return "0手"
    lots = units / lot_size
    if abs(lots - round(lots)) < 1e-6:
        return f"{int(round(lots))}手"
    return f"{lots:.1f}手"


def _max_executable_lots(row: Dict[str, Any]) -> int:
    return max(0, math.floor(abs(_suggested_lots(row))))


def _suggested_lots(row: Dict[str, Any]) -> float:
    op = row.get("operation") or {}
    alloc = _safe_num(op.get("suggested_alloc_cny"))
    price = _safe_num((row.get("price") or {}).get("current"))
    lot_size = _safe_num(row.get("min_lot_size"), 100)
    if alloc == 0 or price <= 0 or lot_size <= 0:
        return 0.0
    return alloc / (price * lot_size)


def _lot_size(row: Dict[str, Any]) -> int:
    return int(_safe_num(row.get("min_lot_size"), 100) or 100)


def _operation_direction(row: Dict[str, Any]) -> str:
    op = row.get("operation") or {}
    verdict = str(op.get("verdict") or "").upper()
    lots = _suggested_lots(row)
    if verdict in {"SELL", "TRIM"} or lots < 0:
        return "SELL"
    return "BUY"


def _execute_user_trade_from_row(row: Dict[str, Any], lots: float) -> str:
    symbol = str(row.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("缺少标的代码")
    if lots <= 0 or int(lots) != lots:
        raise ValueError("手数必须是正整数")
    max_lots = _max_executable_lots(row)
    if max_lots <= 0:
        raise ValueError("推荐手数不足 1 手，已取消记账")
    if lots > max_lots:
        raise ValueError(f"手数不能超过推荐上限 {max_lots} 手")
    from jobs.market_monitor import fetch_sina_prices, load_config

    price_info = fetch_sina_prices([symbol]).get(symbol) or {}
    price = _safe_num(price_info.get("price"))
    if price <= 0:
        raise ValueError("无法获取最新现价，已取消记账")
    units = int(lots) * _lot_size(row)
    direction = _operation_direction(row)
    from db.account_ledger import AccountLedger

    ledger = AccountLedger()
    ledger.ensure_initialized(load_config())
    trade = ledger.apply_user_trade(
        symbol=symbol,
        direction=direction,
        units=units,
        price=price,
        note="monitor_window_user_confirmed",
    )
    name = price_info.get("name") or row.get("name") or symbol
    return f"已按最新价记账: {name} {trade.direction} {trade.units:.0f}股 @ {trade.price:.2f}"


def _fmt_pct(value: Any) -> str:
    return f"{_safe_num(value):+.2f}%"


def _short(text: Any, limit: int = 100) -> str:
    value = str(text or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "..."


def _load_snapshot(path: Path) -> Dict[str, Any]:
    fallback = _load_config_snapshot(f"暂无监控快照: {path}", source_path=path)
    if not path.exists():
        return fallback
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if _looks_like_sample_snapshot(payload):
            return _load_config_snapshot("忽略旧示例快照，已改用本地持仓配置", source_path=path)
        return payload
    except Exception as exc:  # noqa: BLE001
        return _load_config_snapshot(f"读取监控快照失败: {type(exc).__name__}: {exc}", source_path=path)


def _looks_like_sample_snapshot(payload: Dict[str, Any]) -> bool:
    rows = payload.get("rows") or []
    return (
        len(rows) == 1
        and str(rows[0].get("symbol") or "") == "600900"
        and "示例" in str(rows[0].get("name") or "")
    )


def _load_config_snapshot(message: str = "", *, source_path: Optional[Path] = None) -> Dict[str, Any]:
    try:
        from jobs.market_monitor import fetch_sina_prices, load_config
        from jobs.market_monitor import CONFIG_PATH

        config = load_config()
        timestamp_path = source_path if source_path and source_path.exists() else CONFIG_PATH
        stocks = list(config.get("holdings") or []) + list(config.get("watchlist") or [])
        symbols = [str(stock.get("symbol") or "").strip() for stock in stocks if stock.get("symbol")]
        prices = fetch_sina_prices(symbols) if symbols else {}
    except SystemExit:
        config = {}
        stocks = []
        prices = {}
        timestamp_path = source_path
    except Exception:
        config = {}
        stocks = []
        prices = {}
        timestamp_path = source_path

    rows = [_config_stock_row(stock, prices.get(str(stock.get("symbol") or "").strip()) or {}) for stock in stocks]
    return {
        "version": 1,
        "generated_at": _file_timestamp(timestamp_path),
        "round_time": "config",
        "cash_cny": round(_safe_num(config.get("cash")), 2),
        "total_assets_cny": round(_safe_num(config.get("total_assets")), 2),
        "counts": {
            "symbols": len(rows),
            "action_required": 0,
            "entry_exit_alerts": 0,
            "suppressed": 0,
            "errors": 0 if rows else 1,
        },
        "rows": rows,
        "actionable": [],
        "entry_exit_alerts": [],
        "suppressed_alerts": [],
        "message": message,
        "source": "market_monitor_config",
    }


def _file_timestamp(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except Exception:
        return None


def _path_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _config_stock_row(stock: Dict[str, Any], price_info: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(stock.get("symbol") or "").strip()
    position_pct = _safe_num(stock.get("position_pct"))
    return {
        "symbol": symbol,
        "name": stock.get("name") or price_info.get("name") or symbol,
        "market": stock.get("market", "a"),
        "sector": stock.get("sector", ""),
        "industry": stock.get("industry", ""),
        "min_lot_size": int(_safe_num(stock.get("min_lot_size"), 100) or 100),
        "units": round(_safe_num(stock.get("units")), 4),
        "is_holding": position_pct > 0 or _safe_num(stock.get("units")) > 0,
        "position_pct": round(position_pct, 4),
        "target_position_pct": stock.get("target_position_pct", stock.get("target_pct")),
        "cost": _safe_num(stock.get("cost")),
        "price": {
            "current": _safe_num(price_info.get("price")),
            "prev_close": _safe_num(price_info.get("prev_close")),
            "change_pct": _safe_num(price_info.get("change_pct")),
            "name": price_info.get("name", ""),
        },
        "state": "monitoring" if position_pct > 0 else "candidate",
        "buy_criteria": {"pullback_price": 0.0, "breakout_price": 0.0, "reentry_price": 0.0, "reward_risk_ratio": 0.0, "reason": ""},
        "exit_points": {"stop_loss_price": 0.0, "take_profit_price": 0.0, "trim_price": 0.0},
        "fundamental": {"model": "", "score": 50.0, "coverage": 0.0, "anchor_multiplier": 1.0},
        "technical": {
            "regime": "等待交易时段监控刷新",
            "quant_view": "当前显示本地持仓配置",
            "market_data_excerpt": "",
            "entry_exit_model": "",
            "low_confidence": True,
            "atr_pct": 0.0,
            "expected_return_pct": 0.0,
        },
        "operation": {
            "status": "monitoring" if position_pct > 0 else "candidate",
            "reason": "local_config_snapshot",
            "verdict": "HOLD" if position_pct > 0 else "WAIT",
            "confidence": 0.0,
            "suggested_alloc_cny": 0.0,
            "alert_score": 0.0,
            "triggers": [],
            "confirmed": False,
            "llm_conflict": False,
            "execution_blocked": False,
        },
        "llm_review": {"conclusion": "", "one_line": "等待下一次交易时段监控刷新", "risk_note": "", "execution_plan": "", "raw_excerpt": ""},
        "suppressed_reasons": [],
        "error": "",
        "success": True,
    }


def _load_weekend_news_cards(news_dir: Path = WEEKEND_NEWS_DIR) -> tuple[List[Dict[str, Any]], str]:
    candidates = sorted(
        [*news_dir.glob("summary_*.json"), *news_dir.glob("report_*.json")],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else payload
        sectors = summary.get("sector_opportunities") or []
        hot_stocks = summary.get("hot_stock_opportunities") or []
        cards: List[Dict[str, Any]] = []
        for sector in sectors:
            leaders = sector.get("leaders") or []
            cards.append(
                {
                    "title": sector.get("theme") or sector.get("sector") or "周末机会",
                    "sector": sector.get("sector") or "-",
                    "logic": sector.get("logic") or summary.get("summary_one_liner") or "-",
                    "heat_score": _safe_num(sector.get("heat_score")),
                    "freshness_score": _safe_num(sector.get("freshness_score")),
                    "leaders": leaders,
                    "risk_note": "；".join(
                        str(leader.get("risk_note") or "").strip()
                        for leader in leaders
                        if leader.get("risk_note")
                    ),
                }
            )
        if not cards:
            for stock in hot_stocks:
                cards.append(
                    {
                        "title": stock.get("theme") or stock.get("sector") or "周末机会",
                        "sector": stock.get("sector") or "-",
                        "logic": stock.get("reason") or summary.get("summary_one_liner") or "-",
                        "heat_score": _safe_num(stock.get("score")),
                        "freshness_score": 0.0,
                        "leaders": [stock],
                        "risk_note": stock.get("risk_note") or "",
                    }
                )
        if cards:
            return _sort_news_cards(cards), path.name
    return [], "暂无包含板块与龙头股的周末新闻总结"


def _load_daily_selection(path: Path = DAILY_SELECTION_LATEST) -> Dict[str, Any]:
    if not path.exists():
        return {"stocks": [], "message": "暂无日度选股快照"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"stocks": [], "message": f"读取日度选股失败: {type(exc).__name__}: {exc}"}


def _stock_priority_key(row: Dict[str, Any]) -> tuple[Any, ...]:
    state = str(row.get("state") or "")
    operation = row.get("operation") or {}
    verdict = str(operation.get("verdict") or "").upper()
    allocation = abs(_safe_num(operation.get("suggested_alloc_cny")))
    change = abs(_safe_num((row.get("price") or {}).get("change_pct")))
    return (
        STATE_PRIORITY.get(state, 99),
        VERDICT_PRIORITY.get(verdict, 99),
        -allocation,
        -change,
        str(row.get("symbol") or ""),
    )


def _sort_stock_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(rows, key=_stock_priority_key)


def _news_impact_score(card: Dict[str, Any]) -> float:
    leaders = card.get("leaders") or []
    leader_confidence = max(
        (_safe_num(leader.get("confidence")) for leader in leaders),
        default=0.0,
    )
    breadth = min(len(leaders), 4) / 4
    heat = _safe_num(card.get("heat_score"))
    freshness = _safe_num(card.get("freshness_score"))
    return heat * 0.55 + freshness * 0.25 + leader_confidence * 0.15 + breadth * 0.05


def _sort_news_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        cards,
        key=lambda card: (
            -_news_impact_score(card),
            -_safe_num(card.get("heat_score")),
            str(card.get("title") or ""),
        ),
    )


def _demo_rows() -> List[Dict[str, Any]]:
    return [
        {
            "symbol": "600519",
            "name": "贵州茅台",
            "sector": "白酒",
            "state": "monitoring",
            "price": {"current": 1418.20, "change_pct": 1.26},
            "fundamental": {"score": 91},
            "technical": {"regime": "日线企稳，周线震荡"},
            "buy_criteria": {"pullback_price": 1380, "breakout_price": 1435, "reward_risk_ratio": 2.1},
            "exit_points": {"stop_loss_price": 1348, "take_profit_price": 1508},
            "operation": {"verdict": "WAIT", "status": "monitoring", "suggested_alloc_cny": 0},
            "min_lot_size": 100,
            "units": 0,
            "_demo": True,
        },
        {
            "symbol": "603308",
            "name": "应流股份",
            "sector": "商业航天",
            "state": "action_required",
            "price": {"current": 28.64, "change_pct": 4.82},
            "fundamental": {"score": 76},
            "technical": {"regime": "日线突破，周线转强"},
            "buy_criteria": {"pullback_price": 27.90, "breakout_price": 28.50, "reward_risk_ratio": 2.8},
            "exit_points": {"stop_loss_price": 26.80, "take_profit_price": 32.20},
            "operation": {"verdict": "BUY", "status": "action_required", "suggested_alloc_cny": 5728},
            "min_lot_size": 100,
            "units": 200,
            "_demo": True,
        },
    ]


def _demo_news_cards() -> List[Dict[str, Any]]:
    return [
        {
            "title": "商业航天融资升温",
            "sector": "商业航天",
            "logic": "海外商业航天融资与上市预期升温，卫星制造、航天材料和高端铸件可能获得主题催化。",
            "heat_score": 0.86,
            "freshness_score": 0.91,
            "leaders": [{"symbol": "603308", "name": "应流股份"}, {"symbol": "300455", "name": "航天智装"}],
            "risk_note": "海外事件到A股映射存在兑现风险，避免高开追涨。",
        },
        {
            "title": "算电协同政策催化",
            "sector": "算力基础设施",
            "logic": "算力与电力协同政策推动数据中心能源效率升级，电网设备及算力基础设施值得跟踪。",
            "heat_score": 0.78,
            "freshness_score": 0.84,
            "leaders": [{"symbol": "300001", "name": "特锐德"}, {"symbol": "600406", "name": "国电南瑞"}],
            "risk_note": "政策落地节奏与订单兑现仍需验证。",
        },
    ]


def _demo_selection_payload() -> Dict[str, Any]:
    return {
        "trade_date": "2026-06-13",
        "generated_at": "演示数据",
        "stocks": [
            {
                "symbol": "603308",
                "name": "应流股份",
                "sector": "商业航天",
                "score": 86.2,
                "attention": "priority_watch",
                "reasons": ["商业航天热度提升，高端铸件供应链映射清晰", "日线突破，周线转强"],
                "money_flow_score": 88,
                "fundamental_score": 76,
                "evidence_titles": ["商业航天融资升温"],
                "tape": {"interpretation": "放量突破 20 日高点，收盘位置强", "change_pct": 4.82, "tape_score": 82.0},
                "trend": {"interpretation": "日线、周线偏强，月线修复中", "alignment": "partial_bullish"},
                "entry_plan": {"action": "buy_breakout", "trigger_price": 28.5, "stop_loss_price": 26.8, "note": "突破价上方缩量回踩不破可关注"},
            },
            {
                "symbol": "300001",
                "name": "特锐德",
                "sector": "算力基础设施",
                "score": 78.4,
                "attention": "watch",
                "reasons": ["算电协同政策催化，充电网和能源基础设施映射", "日线强于板块但追高风险中等"],
                "money_flow_score": 72,
                "fundamental_score": 68,
                "evidence_titles": ["算电协同政策催化"],
                "tape": {"interpretation": "温和放量上行，未出现明显破位", "change_pct": 2.31, "tape_score": 71.0},
                "trend": {"interpretation": "日线向上，周线震荡，月线未完全确认", "alignment": "mixed"},
                "entry_plan": {"action": "wait_pullback", "trigger_price": 21.2, "stop_loss_price": 19.6, "note": "等待回踩均线区间企稳，不追高"},
            },
        ],
    }


def _label_state(state: Any) -> str:
    return STATE_LABELS.get(str(state or ""), str(state or "-"))


def _state_signal(state: Any) -> str:
    state_key = str(state or "")
    return STATE_SIGNALS.get(state_key, "-")


def _verdict_signal(verdict: Any) -> str:
    verdict_key = str(verdict or "").upper()
    return VERDICT_ARROWS.get(verdict_key, "·")


def _row_tag(row: Dict[str, Any]) -> str:
    state = str(row.get("state") or "")
    change = _safe_num((row.get("price") or {}).get("change_pct"))
    if state == "action_required":
        return "action"
    if state in {"trigger_confirmed", "watch_trigger"}:
        return "trigger"
    if state == "error":
        return "error"
    if state == "blocked":
        return "blocked"
    if change > 0:
        return "up"
    if change < 0:
        return "down"
    return "neutral"


def _state_color(row: Dict[str, Any]) -> str:
    tag = _row_tag(row)
    if tag == "action":
        return ACTION_FG
    if tag == "trigger":
        return TRIGGER_FG
    if tag == "up":
        return UP_FG
    if tag in {"down", "error"}:
        return DOWN_FG
    if tag == "blocked":
        return MUTED
    return BLUE


def _card_bg(row: Dict[str, Any]) -> str:
    tag = _row_tag(row)
    if tag == "action":
        return ACTION_BG
    if tag == "trigger":
        return TRIGGER_BG
    return CARD_BG


def _change_color(row: Dict[str, Any]) -> str:
    change = _safe_num((row.get("price") or {}).get("change_pct"))
    if change > 0:
        return UP_FG
    if change < 0:
        return DOWN_FG
    return MUTED


def _buy_summary(row: Dict[str, Any]) -> str:
    buy = row.get("buy_criteria") or {}
    parts = []
    pullback = _safe_num(buy.get("pullback_price"))
    breakout = _safe_num(buy.get("breakout_price"))
    rr = _safe_num(buy.get("reward_risk_ratio"))
    if pullback > 0:
        parts.append(f"回{pullback:.2f}")
    if breakout > 0:
        parts.append(f"突{breakout:.2f}")
    if rr > 0:
        parts.append(f"盈亏{rr:.1f}")
    return " ".join(parts[:3]) or "-"


def _exit_summary(row: Dict[str, Any]) -> str:
    exit_points = row.get("exit_points") or {}
    parts = []
    stop = _safe_num(exit_points.get("stop_loss_price"))
    take = _safe_num(exit_points.get("take_profit_price"))
    if stop > 0:
        parts.append(f"损{stop:.2f}")
    if take > 0:
        parts.append(f"止{take:.2f}")
    return " ".join(parts[:2]) or "-"


def _operation_summary(row: Dict[str, Any]) -> str:
    op = row.get("operation") or {}
    status = str(op.get("status") or row.get("state") or "")
    lots = _suggested_lots(row)
    verdict = str(op.get("verdict") or "").upper()
    side = "卖" if verdict in {"SELL", "TRIM"} or lots < 0 else "买"
    if status == "action_required":
        return f"{side}{_fmt_lots(abs(lots))}" if lots else "操作"
    if status in {"trigger_confirmed", "watch_trigger"}:
        return "触发"
    if status == "blocked":
        return "拦截"
    return "观察"


def _path_window(path: Dict[str, Any], horizon_days: int) -> Dict[str, Any]:
    for item in path.get("windows") or []:
        if int(_safe_num(item.get("horizon_days"))) == horizon_days:
            return item
    return {}


def _risk_level_label(value: Any) -> str:
    return {
        "low": "低",
        "medium": "中",
        "high": "高",
        "unknown": "未知",
    }.get(str(value or "").lower(), str(value or "-"))


def _path_plain_text(path: Dict[str, Any], risk: Dict[str, Any]) -> tuple[str, bool]:
    window = _path_window(path, 5)
    if not window:
        return "走势概率  暂无足够历史样本", False
    win = _safe_num(window.get("win_probability")) * 100
    tail = _safe_num(window.get("q20_return_pct"))
    risk_level = str(risk.get("risk_level") or "")
    risk_label = _risk_level_label(risk_level)
    if tail < -6:
        tail_note = f"常见回撤可能到 {tail:.1f}%"
    elif tail < 0:
        tail_note = f"回撤压力约 {abs(tail):.1f}%"
    else:
        tail_note = "历史下沿仍为正"
    text = f"走势概率  5日上涨概率 {win:.0f}%  {tail_note}  风险 {risk_label}"
    return text, risk_level == "high"


def _calibration_plain_text(calibration: Dict[str, Any]) -> str:
    sample_size = int(_safe_num(calibration.get("sample_size")))
    hit_rate = calibration.get("hit_rate")
    if sample_size <= 0 or hit_rate is None:
        return "历史验证  相似样本不足，先按低置信度观察"
    avg = _safe_num(calibration.get("avg_forward_return_pct"))
    confidence = _safe_num(calibration.get("confidence_multiplier"), 1.0)
    return f"历史验证  相似样本 {sample_size} 次  5日成功率 {_safe_num(hit_rate) * 100:.0f}%  平均 {avg:.1f}%  置信 {confidence:.2f}"


def _snapshot_detail(row: Dict[str, Any]) -> str:
    op = row.get("operation") or {}
    price = row.get("price") or {}
    fundamental = row.get("fundamental") or {}
    technical = row.get("technical") or {}
    llm = row.get("llm_review") or {}
    triggers = "；".join(
        f"{t.get('side')}:{t.get('kind')}@{_fmt_price(t.get('level'))}"
        for t in op.get("triggers") or []
    ) or "-"
    return "\n".join(
        [
            f"{row.get('name', '-')} ({row.get('symbol', '-')})",
            f"快照状态: {_label_state(row.get('state'))} | 裁决: {op.get('verdict', '-')}",
            f"现价: {_fmt_price(price.get('current'))} | 涨跌: {_fmt_pct(price.get('change_pct'))}",
            f"持仓: {'是' if row.get('is_holding') else '否'} | 仓位: {_safe_num(row.get('position_pct')):.2f}%",
            f"基本面: {_safe_num(fundamental.get('score'), 50):.1f} | 技术: {_short(technical.get('regime'), 100) or '-'}",
            f"买入准则: {_buy_summary(row)}",
            f"出场点: {_exit_summary(row)}",
            f"当前触发: {triggers}",
            f"快照操作: {op.get('reason') or '-'} | 建议: {_operation_summary(row)} | 金额: {_fmt_money(op.get('suggested_alloc_cny'))}",
            f"快照LLM: {llm.get('one_line') or llm.get('conclusion') or '-'}",
        ]
    )


def _committee_action_assessment(row: Dict[str, Any], result: Dict[str, Any]) -> str:
    op = row.get("operation") or {}
    snapshot_verdict = str(op.get("verdict") or "").upper()
    latest_verdict = str(result.get("verdict") or "").upper()
    snapshot_state = str(row.get("state") or "")
    latest_alloc = _safe_num(result.get("suggested_alloc_cny"))
    if not result.get("success"):
        return "委员会分析失败，不能判断当前操作是否恰当。"
    if snapshot_state == "action_required" and latest_verdict in {"BUY", "ACCUMULATE"} and latest_alloc > 0:
        return "当前需要买入/加仓的操作与最新委员会方向一致，但仍需按止损和现金约束执行。"
    if snapshot_state == "action_required" and latest_verdict in {"TRIM", "SELL"}:
        return "快照提示需要操作，但最新委员会偏向减仓/卖出，当前买入类动作不恰当。"
    if snapshot_verdict and latest_verdict and snapshot_verdict != latest_verdict:
        return f"快照裁决为 {snapshot_verdict}，最新委员会裁决为 {latest_verdict}，需要以最新分析为准。"
    if latest_verdict in {"HOLD", "WAIT"} or latest_alloc == 0:
        return "最新委员会没有给出新增仓位，当前更适合观察或等待触发价确认。"
    return "最新委员会仍给出方向性建议，操作前需要核对价格是否仍在买入/出场准则附近。"


def _format_committee_result(row: Dict[str, Any], result: Dict[str, Any]) -> str:
    ee = result.get("entry_exit_points") or {}
    assessment = _committee_action_assessment(row, result)
    lines = [
        "",
        "===== 最新委员会分析 =====",
        f"状态: {'成功' if result.get('success') else '失败'}",
        f"裁决: {result.get('verdict', '-')}",
        f"置信度: {_safe_num(result.get('confidence')):.2f}",
        f"建议金额: {_fmt_money(result.get('suggested_alloc_cny'))}",
        f"基本面: {result.get('fundamental_model') or '-'} / {_safe_num(result.get('fundamental_score'), 50):.1f}",
        f"技术状态: {_short(result.get('regime') or result.get('quant_view'), 220) or '-'}",
        f"买入点: 回踩 {_fmt_price(ee.get('buy_pullback_price'))} / 突破 {_fmt_price(ee.get('buy_breakout_price'))}",
        f"出场点: 止损 {_fmt_price(ee.get('stop_loss_price'))} / 止盈 {_fmt_price(ee.get('take_profit_price'))} / 减仓 {_fmt_price(ee.get('trim_price'))}",
        "",
        "===== 操作是否恰当 =====",
        assessment,
        "",
        "===== LLM 审核摘录 =====",
        _short(result.get("optimizer_review") or result.get("cio_memo") or result.get("error") or "-", 2400),
    ]
    return "\n".join(lines)


def _run_latest_committee_for_row(row: Dict[str, Any]) -> Dict[str, Any]:
    from jobs.market_monitor import (
        _sync_config_account_fields,
        build_holdings_list,
        call_committee,
        fetch_sina_prices,
        load_config,
    )

    symbol = str(row.get("symbol") or "").upper()
    if not symbol:
        return {"success": False, "symbol": symbol, "error": "缺少标的代码"}

    config = load_config()
    try:
        from db.account_ledger import AccountLedger, REAL_ACCOUNT

        ledger = AccountLedger()
        ledger.ensure_initialized(config)
        account_stocks = ledger.stocks_for_committee_input(config, account=REAL_ACCOUNT)
        config = _sync_config_account_fields(config, account_stocks)
    except Exception:
        ledger = None

    holdings = config.get("holdings", [])
    watchlist = config.get("watchlist", [])
    stocks = holdings + watchlist
    stock = next((s for s in stocks if str(s.get("symbol", "")).upper() == symbol), None)
    if not stock:
        stock = {
            "symbol": symbol,
            "name": row.get("name", symbol),
            "market": row.get("market", "a"),
            "position_pct": row.get("position_pct", 0),
            "cost": row.get("cost", 0),
            "sector": row.get("sector", ""),
            "industry": row.get("industry", ""),
        }

    total_assets = float(config.get("total_assets", 0) or 0)
    cash = float(config.get("cash", 0) or 0)
    if ledger is not None:
        try:
            real_summary = ledger.account_summary("real")
            cash = float(real_summary.get("cash_cny", cash) or cash)
        except Exception:
            pass

    price_info = fetch_sina_prices([symbol]).get(symbol) or {}
    current_price = _safe_num(
        price_info.get("price"),
        _safe_num((row.get("price") or {}).get("current")),
    )
    other_holdings = build_holdings_list(holdings, symbol)
    return call_committee(
        symbol=symbol,
        name=str(stock.get("name") or row.get("name") or symbol),
        market=str(stock.get("market") or row.get("market") or "a"),
        position_pct=_safe_num(stock.get("position_pct")),
        cost=_safe_num(stock.get("cost")),
        current_price=current_price,
        total_assets=total_assets,
        cash=cash,
        all_holdings=other_holdings,
        target_position_pct=stock.get("target_position_pct", stock.get("target_pct")),
        t2_pending=float(config.get("t2_pending_cash", 0) or 0),
        sector=str(stock.get("sector") or row.get("sector") or ""),
        industry=str(stock.get("industry") or row.get("industry") or ""),
        fundamentals=stock.get("fundamentals", {}),
        optimizer_review_enabled=True,
    ) or {"success": False, "symbol": symbol, "error": "委员会返回空结果"}


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        text: str,
        command: Any,
        color: str = BLUE,
        hover_color: str = "#2473df",
        width: Optional[int] = None,
        height: int = 42,
        radius: int = 12,
        font: tuple[str, int, str] = ("Microsoft YaHei UI", 10, "bold"),
    ) -> None:
        super().__init__(
            master,
            width=width or height,
            height=height,
            bg=BOARD_BG,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.text = text
        self.command = command
        self.color = color
        self.hover_color = hover_color
        self.radius = radius
        self.font = font
        self.bind("<Configure>", lambda _event: self._draw(self.color))
        self.bind("<Enter>", lambda _event: self._draw(self.hover_color))
        self.bind("<Leave>", lambda _event: self._draw(self.color))
        self.bind("<Button-1>", lambda _event: self.command())

    def _draw(self, color: str) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 2)
        height = max(self.winfo_height(), 2)
        radius = min(self.radius, height // 2, width // 2)
        points = [
            radius, 1, width - radius, 1,
            width - 1, 1, width - 1, radius,
            width - 1, height - radius, width - radius, height - 1,
            radius, height - 1, 1, height - 1,
            1, height - radius, 1, radius,
        ]
        self.create_polygon(points, smooth=True, splinesteps=24, fill=color, outline="")
        self.create_text(
            width / 2,
            height / 2,
            text=self.text,
            fill="#ffffff",
            font=self.font,
        )


class CircleButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        text: str,
        command: Any,
        size: int = 52,
        color: str = BLUE,
        hover_color: str = "#2473df",
    ) -> None:
        super().__init__(
            master,
            width=size,
            height=size,
            bg=BOARD_BG,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.text = text
        self.command = command
        self.size = size
        self.color = color
        self.hover_color = hover_color
        self._normal_image = self._make_image(self.color)
        self._hover_image = self._make_image(self.hover_color)
        self.bind("<Configure>", lambda _event: self._draw(self.color))
        self.bind("<Enter>", lambda _event: self._draw(self.hover_color))
        self.bind("<Leave>", lambda _event: self._draw(self.color))
        self.bind("<Button-1>", lambda _event: self.command())

    def _make_image(self, color: str) -> tk.PhotoImage:
        scale = 4
        image = tk.PhotoImage(width=self.size, height=self.size)
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        radius = (self.size * scale - 2 * scale) / 2
        center = (self.size * scale) / 2
        samples = ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
        for y in range(self.size):
            row = []
            for x in range(self.size):
                inside = 0
                for sx, sy in samples:
                    dx = (x + sx) * scale - center
                    dy = (y + sy) * scale - center
                    if dx * dx + dy * dy <= radius * radius:
                        inside += 1
                alpha = inside / len(samples)
                if alpha == 0:
                    row.append(BOARD_BG)
                elif alpha == 1:
                    row.append(color)
                else:
                    bg = tuple(int(BOARD_BG[i:i + 2], 16) for i in (1, 3, 5))
                    blended = tuple(round(bg[i] * (1 - alpha) + rgb[i] * alpha) for i in range(3))
                    row.append("#%02x%02x%02x" % blended)
            image.put("{" + " ".join(row) + "}", to=(0, y))
        return image

    def _draw(self, color: str) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 2)
        height = max(self.winfo_height(), 2)
        image = self._hover_image if color == self.hover_color else self._normal_image
        self.create_image(width / 2, height / 2, image=image)
        self.create_text(
            width / 2,
            height / 2,
            text=self.text,
            fill="#ffffff",
            font=("Segoe UI", 18, "bold"),
        )


class CashRatioBar(tk.Canvas):
    def __init__(self, master: tk.Misc, *, width: int = 176, height: int = 18) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=BOARD_BG,
            highlightthickness=0,
            bd=0,
        )
        self.cash_text = "现金 -"
        self.ratio = 0.0
        self.bind("<Configure>", lambda _event: self._draw())
        self._draw()

    def set_values(self, cash: Any, total_assets: Any) -> None:
        self.cash_text = _fmt_cash_line(cash, total_assets)
        self.ratio = 1.0 - _cash_ratio(cash, total_assets)
        self._draw()

    def _rounded_rect(self, x1: float, y1: float, x2: float, y2: float, radius: float, *, fill: str, outline: str = "") -> None:
        radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
        points = [
            x1 + radius, y1, x2 - radius, y1,
            x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2 - radius, y2,
            x1 + radius, y2, x1, y2,
            x1, y2 - radius, x1, y1 + radius,
            x1, y1,
        ]
        self.create_polygon(points, smooth=True, splinesteps=18, fill=fill, outline=outline)

    def _draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 2)
        height = max(self.winfo_height(), 2)
        pad = 1
        inner_w = width - pad * 2
        fill_w = max(0, int(inner_w * self.ratio))
        self._rounded_rect(pad, pad, width - pad, height - pad, 7, fill="#e8eef7", outline=LINE)
        if fill_w > 0:
            if fill_w >= inner_w - 2:
                self._rounded_rect(pad + 1, pad + 1, width - pad - 1, height - pad - 1, 6, fill="#9ec5ff")
            else:
                self.create_rectangle(pad + 1, pad + 1, pad + fill_w, height - pad - 1, fill="#9ec5ff", outline="")
        self.create_text(
            width - 9,
            height / 2,
            text=self.cash_text,
            anchor="e",
            fill=TEXT,
            font=("Microsoft YaHei UI", 8, "bold"),
        )


class MonitorWindow:
    def __init__(
        self,
        snapshot_path: Path,
        *,
        poll_ms: int = 1000,
        shake_enabled: bool = True,
        weekend_news_dir: Path = WEEKEND_NEWS_DIR,
        demo: bool = False,
    ) -> None:
        self.snapshot_path = snapshot_path
        self.weekend_news_dir = weekend_news_dir
        self.poll_ms = max(250, poll_ms)
        self.shake_enabled = shake_enabled
        self.demo = demo
        self.root = tk.Tk()
        self.root.title("OpenInvest 监控窗口 - 演示" if demo else "OpenInvest 监控窗口")
        self.root.overrideredirect(True)
        self.closing = False
        self._set_initial_geometry()
        self.root.configure(bg=BOARD_BG)
        self.drag_origin: Optional[tuple[int, int]] = None
        self.pinned = False
        self.previous_action_symbols: set[str] = set()
        self.current_rows: List[Dict[str, Any]] = []
        self.stock_page_index = 0
        self.last_payload_signature: tuple[Any, ...] = ()
        self.watched_mtime_signature: tuple[Any, ...] = ()
        self.news_page_index = 0
        self.stock_content_signature: tuple[Any, ...] = ()
        self.news_content_signature: tuple[Any, ...] = ()
        self.selection_content_signature: tuple[Any, ...] = ()
        self.hover_stack: Optional[str] = None
        self.news_popover: Optional[tk.Frame] = None
        self.news_popover_body: Optional[tk.Frame] = None
        self.news_cards: List[Dict[str, Any]] = []
        self.news_source: str = ""
        self.selection_popover: Optional[tk.Frame] = None
        self.selection_popover_symbol: Optional[str] = None
        self.filter_text = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="等待监控快照")
        self.last_update_text = tk.StringVar(value="更新 -")
        self.card_widgets: Dict[str, tk.Frame] = {}
        self.dialogs: Dict[str, tk.Toplevel] = {}
        self.analysis_queue: queue.Queue[tuple[str, str, Optional[Dict[str, Any]]]] = queue.Queue()
        self._build_ui()

    def _set_initial_geometry(self) -> None:
        screen_w = self.root.winfo_screenwidth()
        width = min(max(330, screen_w // 4), 420)
        height = 332
        self.root.geometry(f"{width}x{height}+24+32")
        self.root.minsize(320, 260)

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("App.TFrame", background=BOARD_BG)
        style.configure("Header.TFrame", background=BOARD_BG)
        style.configure("Filter.TFrame", background=BOARD_BG)
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("TLabel", background=BOARD_BG, foreground=TEXT, font=("Microsoft YaHei UI", 10))
        style.configure("HeaderTitle.TLabel", background=BOARD_BG, foreground=TEXT, font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("HeaderMeta.TLabel", background=BOARD_BG, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Hint.TLabel", background=BOARD_BG, foreground=MUTED, font=("Microsoft YaHei UI", 9))

        titlebar = tk.Frame(self.root, bg=PANEL_BG, height=34)
        titlebar.pack(fill=tk.X)
        titlebar.pack_propagate(False)
        brand = tk.Label(
            titlebar,
            text="OpenInvest",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=12,
        )
        brand.pack(side=tk.LEFT, fill=tk.Y)
        title_hint = tk.Label(
            titlebar,
            text="演示模式" if self.demo else "实时监控",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        )
        title_hint.pack(side=tk.LEFT)
        close_btn = tk.Label(
            titlebar,
            text="×",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI", 14),
            width=4,
            cursor="hand2",
        )
        close_btn.pack(side=tk.RIGHT, fill=tk.Y)
        min_btn = tk.Label(
            titlebar,
            text="−",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI", 13),
            width=4,
            cursor="hand2",
        )
        min_btn.pack(side=tk.RIGHT, fill=tk.Y)
        self.pin_btn = tk.Label(
            titlebar,
            text="📌",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI Emoji", 11),
            width=4,
            cursor="hand2",
        )
        self.pin_btn.pack(side=tk.RIGHT, fill=tk.Y)
        close_btn.bind("<Button-1>", lambda _event: self._on_close())
        min_btn.bind("<Button-1>", lambda _event: self._minimize())
        self.pin_btn.bind("<Button-1>", lambda _event: self._toggle_pin())
        close_btn.bind("<Enter>", lambda _event: close_btn.configure(bg="#e5484d", fg="#ffffff"))
        close_btn.bind("<Leave>", lambda _event: close_btn.configure(bg=PANEL_BG, fg=MUTED))
        min_btn.bind("<Enter>", lambda _event: min_btn.configure(bg="#eef2f7", fg=TEXT))
        min_btn.bind("<Leave>", lambda _event: min_btn.configure(bg=PANEL_BG, fg=MUTED))
        self.pin_btn.bind("<Enter>", lambda _event: self.pin_btn.configure(bg="#eef2f7"))
        self.pin_btn.bind("<Leave>", lambda _event: self.pin_btn.configure(bg=PANEL_BG))
        for widget in (titlebar, brand, title_hint):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag_window)

        separator = tk.Frame(self.root, bg=LINE, height=1)
        separator.pack(fill=tk.X)

        top = tk.Frame(self.root, bg=BOARD_BG, padx=12, pady=6)
        top.pack(fill=tk.X, pady=(1, 0))
        tk.Label(
            top,
            textvariable=self.last_update_text,
            bg=BOARD_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side=tk.LEFT)
        self.cash_bar = CashRatioBar(top, width=226, height=18)
        self.cash_bar.pack(side=tk.RIGHT)

        search_wrap = tk.Frame(self.root, bg="#eef2f7", padx=10, pady=5)
        search_wrap.pack(fill=tk.X, padx=12, pady=(2, 7))
        tk.Label(search_wrap, text="⌕", bg="#eef2f7", fg="#98a2b3", font=("Microsoft YaHei UI", 11)).pack(side=tk.LEFT)
        search = tk.Entry(
            search_wrap,
            textvariable=self.filter_text,
            bd=0,
            bg="#eef2f7",
            fg=TEXT,
            insertbackground=TEXT,
            font=("Microsoft YaHei UI", 10),
        )
        search.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        section = tk.Frame(self.root, bg=BOARD_BG, padx=14)
        section.pack(fill=tk.X)
        tk.Label(section, text="标的监控", bg=BOARD_BG, fg=MUTED, font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT)
        news_btn = tk.Label(
            section,
            text="周末新闻",
            bg=BLUE,
            fg="#ffffff",
            font=("Microsoft YaHei UI", 8, "bold"),
            padx=12,
            pady=4,
            cursor="hand2",
        )
        news_btn.pack(side=tk.RIGHT, padx=(8, 0))
        news_btn.bind("<Button-1>", lambda _event: self._open_weekend_news())
        news_btn.bind("<Enter>", lambda _event: news_btn.configure(bg="#2473df"))
        news_btn.bind("<Leave>", lambda _event: news_btn.configure(bg=BLUE))
        tk.Label(section, textvariable=self.status_text, bg=BOARD_BG, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT)
        self.filter_text.trace_add("write", lambda *_: self._reset_stock_page())

        self.selection_bar = tk.Frame(self.root, bg=BOARD_BG, padx=12)
        self.selection_bar.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 7))
        tk.Label(
            self.selection_bar,
            text="选股",
            bg=BOARD_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.selection_buttons_frame = tk.Frame(self.selection_bar, bg=BOARD_BG)
        self.selection_buttons_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.canvas = tk.Canvas(self.root, bg=BOARD_BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 6))
        self.cards_frame = tk.Frame(self.canvas, bg=BOARD_BG)
        self.cards_window = self.canvas.create_window((0, 0), window=self.cards_frame, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.cards_frame.bind("<Configure>", self._on_cards_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        self.refresh_fab = CircleButton(
            self.root,
            text="↻",
            command=self.refresh,
            size=46,
        )
        self.refresh_fab.place(relx=1.0, rely=1.0, x=-16, y=-16, anchor="se")

    def _start_drag(self, event: tk.Event) -> None:
        self.drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_window(self, event: tk.Event) -> None:
        if self.drag_origin is None:
            return
        x = event.x_root - self.drag_origin[0]
        y = event.y_root - self.drag_origin[1]
        self.root.geometry(f"+{x}+{y}")

    def _minimize(self) -> None:
        self.root.overrideredirect(False)
        self.root.iconify()
        self.root.bind("<Map>", self._restore_custom_chrome, add="+")

    def _restore_custom_chrome(self, _event: tk.Event) -> None:
        self.root.after(10, lambda: self.root.overrideredirect(True))

    def _toggle_pin(self) -> None:
        self.pinned = not self.pinned
        self.root.attributes("-topmost", self.pinned)
        self.pin_btn.configure(fg=BLUE if self.pinned else MUTED)

    def start(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh()
        self._watch_snapshot_changes()
        self._poll_analysis_queue()
        self.root.mainloop()

    def _on_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.status_text.set("正在停止后台任务...")
        if not self.demo:
            threading.Thread(target=self._shutdown_and_destroy, daemon=True).start()
        else:
            self.root.destroy()

    def _shutdown_and_destroy(self) -> None:
        _stop_background_services()
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            pass

    def _watch_snapshot_changes(self) -> None:
        if self.closing:
            return
        signature = self._watched_file_signature()
        if self.watched_mtime_signature and signature != self.watched_mtime_signature:
            self.refresh()
        self.watched_mtime_signature = signature
        self.root.after(self.poll_ms, self._watch_snapshot_changes)

    def _watched_file_signature(self) -> tuple[Any, ...]:
        return (
            _path_mtime(self.snapshot_path),
            _path_mtime(ROOT / "jobs" / "market_monitor_config.json"),
            _path_mtime(DAILY_SELECTION_LATEST),
        )

    def refresh(self) -> None:
        payload = (
            {
                "rows": _demo_rows(),
                "counts": {"symbols": 2, "action_required": 1},
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "cash_cny": 28600,
                "total_assets_cny": 100000,
            }
            if self.demo
            else _load_snapshot(self.snapshot_path)
        )
        rows = _sort_stock_rows(list(payload.get("rows") or []))
        stock_signature = tuple(
            (
                row.get("symbol"),
                row.get("name"),
                row.get("state"),
                (row.get("operation") or {}).get("verdict"),
                _safe_num((row.get("operation") or {}).get("suggested_alloc_cny")),
                _safe_num((row.get("price") or {}).get("current")),
                _safe_num((row.get("price") or {}).get("change_pct")),
                _safe_num(row.get("position_pct")),
                _safe_num(row.get("units")),
            )
            for row in rows
        )
        counts = payload.get("counts") or {}
        payload_signature = (
            payload.get("generated_at"),
            payload.get("round_time"),
            _safe_num(payload.get("cash_cny")),
            _safe_num(payload.get("total_assets_cny")),
            counts.get("symbols", len(rows)),
            counts.get("action_required", 0),
            stock_signature,
        )
        if payload_signature == self.last_payload_signature:
            self._place_refresh_fab()
            self._refresh_open_news_popover()
            self._render_selection_buttons()
            return

        if self.stock_content_signature and stock_signature != self.stock_content_signature:
            self.stock_page_index = 0
        self.stock_content_signature = stock_signature
        self.last_payload_signature = payload_signature
        self.current_rows = rows
        self.status_text.set(
            f"{counts.get('symbols', len(rows))} 标的 / {counts.get('action_required', 0)} 操作"
        )
        self.last_update_text.set(_fmt_update_time(payload.get("generated_at")))
        self.cash_bar.set_values(payload.get("cash_cny"), payload.get("total_assets_cny"))
        self._render_rows()
        self._resize_to_rows(len(self._filtered_rows()))
        self._place_refresh_fab()
        self._maybe_alert(rows)
        self._refresh_open_news_popover()
        self._render_selection_buttons()

    def _resize_to_rows(self, row_count: int) -> None:
        screen_h = self.root.winfo_screenheight()
        screen_w = self.root.winfo_screenwidth()
        height = min(420 if row_count > 1 else 390, max(306, screen_h - 120))
        width = min(max(330, screen_w // 4), 420)
        x = self.root.winfo_x() if self.root.winfo_x() >= 0 else 24
        y = self.root.winfo_y() if self.root.winfo_y() >= 0 else 32
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.after(10, self._place_refresh_fab)

    def _place_refresh_fab(self) -> None:
        if hasattr(self, "refresh_fab"):
            self.refresh_fab._draw(self.refresh_fab.color)
            self.root.tk.call("raise", self.refresh_fab._w)
            self.refresh_fab.place(relx=1.0, rely=1.0, x=-16, y=-16, anchor="se")

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.cards_window, width=event.width)
        self._place_refresh_fab()

    def _on_cards_configure(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_mousewheel(self, event: tk.Event) -> None:
        if self.hover_stack == "news":
            self._change_news_page(-1 if event.delta > 0 else 1)
            return
        if self.hover_stack != "stocks":
            return
        rows = self._filtered_rows()
        if len(rows) > 1:
            self._change_stock_page(-1 if event.delta > 0 else 1)

    def _filtered_rows(self) -> List[Dict[str, Any]]:
        query = self.filter_text.get().strip().lower()
        out = []
        for row in self.current_rows:
            haystack = " ".join(
                str(row.get(key, ""))
                for key in ("symbol", "name", "sector", "industry", "state")
            ).lower()
            verdict = str((row.get("operation") or {}).get("verdict", "")).lower()
            if query and query not in haystack and query not in verdict:
                continue
            out.append(row)
        return out

    def _render_rows(self, slide_step: int = 0) -> None:
        for child in self.cards_frame.winfo_children():
            child.destroy()
        self.card_widgets.clear()
        rows = self._filtered_rows()
        if rows:
            self.stock_page_index %= len(rows)
            self._create_stack_layers(self.cards_frame, len(rows))
            stage = self._create_card_stage(self.cards_frame, STOCK_CARD_STAGE_HEIGHT, pady=(0, 6))
            card = self._create_card(rows[self.stock_page_index], parent=stage, managed=False)
            self._place_card(stage, card, slide_step)
            if len(rows) > 1:
                self._create_pager(
                    self.cards_frame,
                    self.stock_page_index,
                    len(rows),
                    lambda: self._change_stock_page(-1),
                    lambda: self._change_stock_page(1),
                )
        else:
            empty = tk.Label(
                self.cards_frame,
                text="没有匹配的标的",
                bg=BOARD_BG,
                fg=MUTED,
                font=("Microsoft YaHei UI", 10),
                pady=18,
            )
            empty.pack(fill=tk.X)
        self.cards_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _reset_stock_page(self) -> None:
        self.stock_page_index = 0
        self._render_rows()

    def _change_stock_page(self, step: int) -> None:
        rows = self._filtered_rows()
        if len(rows) <= 1:
            return
        self.stock_page_index = (self.stock_page_index + step) % len(rows)
        self._render_rows(slide_step=step)

    def _create_card_stage(
        self,
        parent: tk.Misc,
        height: int,
        *,
        pady: tuple[int, int] = (0, 0),
        expand: bool = False,
    ) -> tk.Frame:
        stage = tk.Frame(parent, bg=BOARD_BG, height=height)
        stage.pack(fill=tk.BOTH if expand else tk.X, expand=expand, pady=pady)
        stage.pack_propagate(False)
        return stage

    def _place_card(self, stage: tk.Frame, widget: tk.Widget, direction: int = 0) -> None:
        widget.place(x=0, y=0, relwidth=1)
        if direction:
            self._animate_slide(widget, direction)

    def _animate_slide(self, widget: tk.Widget, direction: int) -> None:
        offsets = [46, 31, 19, 10, 4, 0]

        def step(index: int = 0) -> None:
            if not widget.winfo_exists():
                return
            offset = offsets[index]
            y = offset if direction > 0 else -offset
            widget.place_configure(y=y)
            if index + 1 < len(offsets):
                widget.after(18, lambda: step(index + 1))
            else:
                widget.place_configure(y=0)

        step()

    def _create_stack_layers(self, parent: tk.Misc, count: int) -> None:
        if count <= 1:
            return
        back = tk.Frame(parent, bg="#dce6f5", height=6)
        back.pack(fill=tk.X, padx=12, pady=(3, 0))
        back.pack_propagate(False)
        middle = tk.Frame(parent, bg="#e7eef8", height=5)
        middle.pack(fill=tk.X, padx=6)
        middle.pack_propagate(False)

    def _create_pager(
        self,
        parent: tk.Misc,
        index: int,
        total: int,
        previous: Any,
        following: Any,
    ) -> None:
        pager = tk.Frame(parent, bg=BOARD_BG)
        pager.pack(fill=tk.X, pady=(0, 0))
        prev = tk.Label(pager, text="‹", bg=BOARD_BG, fg=BLUE, font=("Segoe UI", 18, "bold"), cursor="hand2", width=3)
        prev.pack(side=tk.LEFT)
        tk.Label(
            pager,
            text=f"{index + 1} / {total}",
            bg=BOARD_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(side=tk.LEFT, expand=True)
        nxt = tk.Label(pager, text="›", bg=BOARD_BG, fg=BLUE, font=("Segoe UI", 18, "bold"), cursor="hand2", width=3)
        nxt.pack(side=tk.RIGHT)
        prev.bind("<Button-1>", lambda _event: previous())
        nxt.bind("<Button-1>", lambda _event: following())

    def _create_card(
        self,
        row: Dict[str, Any],
        *,
        parent: Optional[tk.Misc] = None,
        managed: bool = True,
    ) -> tk.Frame:
        symbol = str(row.get("symbol") or "")
        op = row.get("operation") or {}
        price = row.get("price") or {}
        fundamental = row.get("fundamental") or {}
        bg = _card_bg(row)
        state_color = _state_color(row)
        change_color = _change_color(row)

        card = tk.Frame(parent or self.cards_frame, bg=bg, padx=10, pady=7, cursor="hand2")
        if managed:
            card.pack(fill=tk.X, pady=(0, 8))
        card.bind("<Enter>", lambda _event: self._set_hover_stack("stocks"))
        card.bind("<Leave>", lambda _event: self._clear_hover_stack("stocks"))
        card.bind("<MouseWheel>", lambda event: self._wheel_stock_stack(event))
        self.card_widgets[symbol] = card

        top = tk.Frame(card, bg=bg)
        top.pack(fill=tk.X)
        tk.Label(top, text="●", bg=bg, fg=state_color, font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT)
        tk.Label(
            top,
            text=f"  {row.get('name', symbol)}",
            bg=bg,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(top, text=symbol, bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(
            top,
            text=_operation_summary(row),
            bg=SOFT_BLUE if _row_tag(row) != "action" else "#ffe5e0",
            fg=ACTION_FG if _row_tag(row) == "action" else BLUE,
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=8,
            pady=2,
        ).pack(side=tk.RIGHT)

        trade_bar = tk.Frame(card, bg=bg)
        trade_bar.pack(fill=tk.X, pady=(6, 0))
        max_lots = _max_executable_lots(row)
        lots_var = tk.StringVar(value=str(max_lots) if max_lots > 0 else "0")
        side_text = "已执行卖出" if _operation_direction(row) == "SELL" else "已执行买入"
        status_var = tk.StringVar(value="")
        tk.Label(trade_bar, text="手", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        lots_entry = tk.Entry(
            trade_bar,
            textvariable=lots_var,
            width=5,
            bd=0,
            bg="#ffffff" if bg != CARD_BG else "#eef2f7",
            fg=TEXT,
            justify=tk.CENTER,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        lots_entry.pack(side=tk.LEFT, padx=(4, 8), ipady=2)
        exec_btn = tk.Label(
            trade_bar,
            text=side_text,
            bg=BLUE,
            fg="#ffffff",
            font=("Microsoft YaHei UI", 8, "bold"),
            padx=8,
            pady=3,
            cursor="hand2",
        )
        exec_btn.pack(side=tk.LEFT)
        tk.Label(trade_bar, textvariable=status_var, bg=bg, fg=UP_FG, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT)

        def confirm_trade(_event: Optional[tk.Event] = None, *, r: Dict[str, Any] = row) -> str:
            try:
                if r.get("_demo"):
                    raise ValueError("演示模式不会更新持仓")
                raw_lots = lots_var.get().strip()
                if not raw_lots.isdigit():
                    raise ValueError("手数必须是整数")
                lots = int(raw_lots)
                message = _execute_user_trade_from_row(r, lots)
                status_var.set("已更新")
                self.status_text.set(message)
                self.refresh()
                return "break"
            except Exception as exc:  # noqa: BLE001
                status_var.set("失败")
                self.status_text.set(f"记账失败: {exc}")
                return "break"

        exec_btn.bind("<Button-1>", confirm_trade)
        lots_entry.bind("<Return>", confirm_trade)

        mid = tk.Frame(card, bg=bg)
        mid.pack(fill=tk.X, pady=(6, 0))
        tk.Label(mid, text=_fmt_price(price.get("current")), bg=bg, fg=TEXT, font=("Microsoft YaHei UI", 14, "bold")).pack(side=tk.LEFT)
        tk.Label(mid, text=_fmt_pct(price.get("change_pct")), bg=bg, fg=change_color, font=("Microsoft YaHei UI", 10, "bold")).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(mid, text=f"{_verdict_signal(op.get('verdict'))} {op.get('verdict', '-')}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side=tk.RIGHT)

        bottom = tk.Frame(card, bg=bg)
        bottom.pack(fill=tk.X, pady=(4, 0))
        tk.Label(bottom, text=f"买 {_buy_summary(row)}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        tk.Label(bottom, text=f"卖 {_exit_summary(row)}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(bottom, text=f"基 {float(_safe_num(fundamental.get('score'), 50)):.0f}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT)
        tk.Label(bottom, text=f"持 {_fmt_holding_lots(row)}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT, padx=(0, 10))

        for widget in (card, top, mid, bottom):
            widget.bind("<Button-1>", lambda _event, s=symbol: self._open_analysis_dialog(s))
        for widget in card.winfo_children():
            if widget is trade_bar:
                continue
            widget.bind("<Button-1>", lambda _event, s=symbol: self._open_analysis_dialog(s))
            for nested in widget.winfo_children():
                nested.bind("<Button-1>", lambda _event, s=symbol: self._open_analysis_dialog(s))
                nested.bind("<Enter>", lambda _event: self._set_hover_stack("stocks"))
                nested.bind("<MouseWheel>", lambda event: self._wheel_stock_stack(event))
        return card

    def _set_hover_stack(self, stack: str) -> None:
        self.hover_stack = stack

    def _clear_hover_stack(self, stack: str) -> None:
        if self.hover_stack == stack:
            self.hover_stack = None

    def _wheel_stock_stack(self, event: tk.Event) -> str:
        rows = self._filtered_rows()
        if len(rows) > 1:
            self._change_stock_page(-1 if event.delta > 0 else 1)
        return "break"

    def _wheel_news_stack(self, event: tk.Event) -> str:
        self._change_news_page(-1 if event.delta > 0 else 1)
        return "break"

    def _open_weekend_news(self) -> None:
        if self.news_popover and self.news_popover.winfo_exists():
            self._close_news_popover()
            return
        cards, source = self._current_news_cards()
        self.news_cards = cards
        self.news_source = source
        popover = tk.Frame(self.root, bg=LINE, padx=1, pady=1)
        self.news_popover = popover
        panel = tk.Frame(popover, bg=PANEL_BG, padx=12, pady=10)
        panel.pack(fill=tk.BOTH, expand=True)
        top = tk.Frame(panel, bg=PANEL_BG)
        top.pack(fill=tk.X)
        tk.Label(top, text="周末新闻机会", bg=PANEL_BG, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(side=tk.LEFT)
        tk.Label(top, text=source, bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=(8, 0))
        close_btn = tk.Label(top, text="×", bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 12), width=2, cursor="hand2")
        close_btn.pack(side=tk.RIGHT)
        close_btn.bind("<Button-1>", lambda _event: self._close_news_popover())
        body = tk.Frame(panel, bg=BOARD_BG)
        body.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.news_popover_body = body
        self.news_page_index = 0
        self.news_content_signature = self._news_signature(cards, source)
        self._render_news_page()
        self.root.update_idletasks()
        width = min(380, max(310, self.root.winfo_width() - 28))
        height = 285
        x = 14
        y = max(92, self.selection_bar.winfo_y() - height - 38)
        popover.place(x=x, y=y, width=width, height=height)
        popover.lift()
        self.root.tk.call("raise", popover._w)

    def _current_news_cards(self) -> tuple[List[Dict[str, Any]], str]:
        if self.demo:
            return _sort_news_cards(_demo_news_cards()), "演示数据：2 条周末新闻"
        return _load_weekend_news_cards(self.weekend_news_dir)

    def _news_signature(self, cards: List[Dict[str, Any]], source: str) -> tuple[Any, ...]:
        return (
            source,
            *(
                (
                    card.get("title"),
                    card.get("sector"),
                    _news_impact_score(card),
                    tuple(leader.get("symbol") for leader in card.get("leaders") or []),
                )
                for card in cards
            ),
        )

    def _refresh_open_news_popover(self) -> None:
        if not self.news_popover or not self.news_popover.winfo_exists():
            return
        cards, source = self._current_news_cards()
        signature = self._news_signature(cards, source)
        if signature == self.news_content_signature:
            return
        self.news_content_signature = signature
        self.news_page_index = 0
        self.news_cards = cards
        self.news_source = source
        self._render_news_page()

    def _close_news_popover(self) -> None:
        if self.news_popover and self.news_popover.winfo_exists():
            self.news_popover.destroy()
        self.news_popover = None
        self.news_popover_body = None
        self.news_cards = []
        self.news_source = ""

    def _current_selection_payload(self) -> Dict[str, Any]:
        return _demo_selection_payload() if self.demo else _load_daily_selection()

    def _selection_stocks(self) -> List[Dict[str, Any]]:
        payload = self._current_selection_payload()
        return sorted(payload.get("stocks") or [], key=lambda row: (-_safe_num(row.get("score")), str(row.get("symbol") or "")))[:6]

    def _render_selection_buttons(self) -> None:
        if not hasattr(self, "selection_buttons_frame"):
            return
        stocks = self._selection_stocks()
        signature = tuple((stock.get("symbol"), stock.get("score")) for stock in stocks)
        if signature == self.selection_content_signature:
            return
        self.selection_content_signature = signature
        self._close_selection_popover()
        for child in self.selection_buttons_frame.winfo_children():
            child.destroy()
        if not stocks:
            tk.Label(
                self.selection_buttons_frame,
                text="暂无",
                bg=BOARD_BG,
                fg=MUTED,
                font=("Microsoft YaHei UI", 8),
            ).pack(side=tk.LEFT)
            return
        for stock in stocks:
            label = tk.Label(
                self.selection_buttons_frame,
                text=f"{stock.get('name') or stock.get('symbol')}",
                bg="#eef3f8",
                fg=TEXT,
                font=("Microsoft YaHei UI", 8),
                padx=12,
                pady=5,
                relief=tk.SOLID,
                bd=1,
                highlightthickness=1,
                highlightbackground="#9aa9bb",
                cursor="hand2",
            )
            label.pack(side=tk.LEFT, padx=(0, 6), pady=(1, 1))
            label.bind("<Button-1>", lambda _event, s=stock, w=label: self._show_selection_popover(s, w))
            label.bind("<Enter>", lambda _event, w=label: w.configure(bg="#e2e8f0"))
            label.bind("<Leave>", lambda _event, w=label: w.configure(bg="#eef3f8"))

    def _show_selection_popover(self, stock: Dict[str, Any], anchor: tk.Widget) -> None:
        symbol = str(stock.get("symbol") or "")
        if (
            self.selection_popover
            and self.selection_popover.winfo_exists()
            and self.selection_popover_symbol == symbol
        ):
            self._close_selection_popover()
            return
        self._close_selection_popover()
        popover = tk.Frame(self.root, bg=LINE, padx=1, pady=1)
        self.selection_popover = popover
        self.selection_popover_symbol = symbol

        panel = tk.Frame(popover, bg=PANEL_BG, padx=12, pady=10)
        panel.pack(fill=tk.BOTH, expand=True)
        top = tk.Frame(panel, bg=PANEL_BG)
        top.pack(fill=tk.X)
        tk.Label(
            top,
            text=f"{stock.get('name') or symbol}  {symbol}",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(top, text=f"{_safe_num(stock.get('score')):.0f}", bg="#ffe5e0", fg=DOWN_FG, font=("Microsoft YaHei UI", 8, "bold"), padx=8, pady=2).pack(side=tk.LEFT, padx=(8, 0))
        close_btn = tk.Label(top, text="×", bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 12), width=2, cursor="hand2")
        close_btn.pack(side=tk.RIGHT)
        close_btn.bind("<Button-1>", lambda _event: self._close_selection_popover())

        self._render_selection_reason(panel, stock)
        pointer = tk.Canvas(popover, width=24, height=12, bg=BOARD_BG, highlightthickness=0, bd=0)
        pointer.create_polygon(2, 0, 22, 0, 12, 12, fill=LINE, outline=LINE)
        pointer.place(relx=0.5, rely=1.0, y=-1, anchor="n")
        self.root.update_idletasks()
        width = min(370, max(300, self.root.winfo_width() - 28))
        height = 250
        x = max(12, min(anchor.winfo_x(), self.root.winfo_width() - width - 12))
        y = max(120, self.selection_bar.winfo_y() - height - 8)
        popover.place(x=x, y=y, width=width, height=height)
        popover.lift()
        self.root.tk.call("raise", popover._w)

    def _close_selection_popover(self) -> None:
        if self.selection_popover and self.selection_popover.winfo_exists():
            self.selection_popover.destroy()
        self.selection_popover = None
        self.selection_popover_symbol = None

    def _render_selection_reason(self, body: tk.Frame, stock: Dict[str, Any]) -> None:
        tape = stock.get("tape") or {}
        trend = stock.get("trend") or {}
        plan = stock.get("entry_plan") or {}
        path = stock.get("path_distribution") or {}
        risk = stock.get("risk_defense") or {}
        calibration = stock.get("calibration") or {}
        card = tk.Frame(body, bg=PANEL_BG)
        card.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        tk.Label(card, text=f"板块  {stock.get('sector', '-')}", bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 8, "bold"), anchor="w").pack(fill=tk.X)
        reasons = "；".join(str(x) for x in (stock.get("reasons") or [])[:3])
        tk.Label(card, text=f"原因  {_short(reasons, 120) or '-'}", bg=PANEL_BG, fg=TEXT, font=("Microsoft YaHei UI", 8), justify=tk.LEFT, anchor="w", wraplength=330).pack(fill=tk.X, pady=(6, 0))
        tk.Label(
            card,
            text=f"资金/基本面  {_safe_num(stock.get('money_flow_score')):.0f} / {_safe_num(stock.get('fundamental_score'), 50):.0f}",
            bg=PANEL_BG,
            fg=DOWN_FG,
            font=("Microsoft YaHei UI", 8, "bold"),
            anchor="w",
        ).pack(fill=tk.X, pady=(7, 0))
        tk.Label(card, text=f"走势  {_short(tape.get('interpretation') or trend.get('interpretation'), 105)}", bg=PANEL_BG, fg=BLUE, font=("Microsoft YaHei UI", 8, "bold"), justify=tk.LEFT, anchor="w", wraplength=330).pack(fill=tk.X, pady=(7, 0))
        path_text, is_high_risk = _path_plain_text(path, risk)
        tk.Label(
            card,
            text=path_text,
            bg=PANEL_BG,
            fg=DOWN_FG if is_high_risk else TEXT,
            font=("Microsoft YaHei UI", 8, "bold"),
            anchor="w",
            justify=tk.LEFT,
            wraplength=330,
        ).pack(fill=tk.X, pady=(7, 0))
        tk.Label(
            card,
            text=_calibration_plain_text(calibration),
            bg=PANEL_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            justify=tk.LEFT,
            wraplength=330,
        ).pack(fill=tk.X, pady=(6, 0))
        trigger = _fmt_price(plan.get("trigger_price"))
        stop = _fmt_price(plan.get("stop_loss_price"))
        tk.Label(card, text=f"买点  {plan.get('action', '-')}  触发 {trigger}  止损 {stop}", bg=PANEL_BG, fg=UP_FG, font=("Microsoft YaHei UI", 8, "bold"), anchor="w").pack(fill=tk.X, pady=(7, 0))
        tk.Label(card, text=f"备注  {_short(plan.get('note'), 92) or '-'}", bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 8), justify=tk.LEFT, anchor="w", wraplength=330).pack(fill=tk.X, pady=(6, 0))
        status_var = tk.StringVar(value="")
        action_bar = tk.Frame(body, bg=PANEL_BG)
        action_bar.pack(fill=tk.X, pady=(8, 0))
        add_btn = tk.Label(
            action_bar,
            text="加入关注列表",
            bg="#eef3f8",
            fg=TEXT,
            font=("Microsoft YaHei UI", 8),
            padx=12,
            pady=5,
            relief=tk.SOLID,
            bd=1,
            highlightthickness=1,
            highlightbackground="#9aa9bb",
            cursor="hand2",
        )
        add_btn.pack(side=tk.LEFT)
        tk.Label(action_bar, textvariable=status_var, bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=(10, 0))

        def add_to_watchlist(_event: Optional[tk.Event] = None) -> str:
            try:
                message = self._add_selection_to_watchlist(stock)
                status_var.set(message)
                self.status_text.set(message)
                return "break"
            except Exception as exc:  # noqa: BLE001
                status_var.set(f"失败: {exc}")
                return "break"

        add_btn.bind("<Button-1>", add_to_watchlist)
        add_btn.bind("<Enter>", lambda _event: add_btn.configure(bg="#e2e8f0"))
        add_btn.bind("<Leave>", lambda _event: add_btn.configure(bg="#eef3f8"))

    def _add_selection_to_watchlist(self, stock: Dict[str, Any]) -> str:
        if self.demo:
            return "演示模式未写入关注列表"
        symbol = str(stock.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("缺少股票代码")
        from jobs.market_monitor import CONFIG_PATH, load_config

        config = load_config()
        watchlist = list(config.get("watchlist") or [])
        all_symbols = {
            str(item.get("symbol") or "").strip()
            for item in list(config.get("holdings") or []) + watchlist
        }
        if symbol in all_symbols:
            return "已在持仓或关注列表"
        watchlist.append(
            {
                "symbol": symbol,
                "name": stock.get("name") or symbol,
                "market": "a",
                "sector": stock.get("sector") or "",
                "industry": "",
                "position_pct": 0,
                "cost": 0,
            }
        )
        config["watchlist"] = watchlist
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"已加入关注列表: {stock.get('name') or symbol}"

    def _change_news_page(self, step: int) -> None:
        if not self.news_popover or not self.news_popover.winfo_exists():
            return
        cards = self.news_cards
        if len(cards) <= 1:
            return
        self.news_page_index = (self.news_page_index + step) % len(cards)
        self._render_news_page(slide_step=step)

    def _render_news_page(self, slide_step: int = 0) -> None:
        if not self.news_popover or not self.news_popover.winfo_exists() or self.news_popover_body is None:
            return
        body = self.news_popover_body
        cards = self.news_cards
        for child in body.winfo_children():
            child.destroy()
        if not cards:
            tk.Label(body, text="暂无包含板块与龙头股的周末新闻总结", bg=BOARD_BG, fg=MUTED, font=("Microsoft YaHei UI", 10)).pack(expand=True)
            return
        self.news_page_index %= len(cards)
        self._create_stack_layers(body, len(cards))
        stage = self._create_card_stage(body, NEWS_CARD_STAGE_HEIGHT, expand=True)
        card = self._create_news_card(stage, cards[self.news_page_index], managed=False)
        self._place_card(stage, card, slide_step)
        if len(cards) > 1:
            self._create_pager(
                body,
                self.news_page_index,
                len(cards),
                lambda: self._change_news_page(-1),
                lambda: self._change_news_page(1),
            )

    def _create_news_card(
        self,
        parent: tk.Misc,
        card_data: Dict[str, Any],
        *,
        managed: bool = True,
    ) -> tk.Frame:
        heat = _safe_num(card_data.get("heat_score"))
        heat_color = ACTION_FG if heat >= 0.8 else TRIGGER_FG if heat >= 0.6 else BLUE
        card = tk.Frame(parent, bg=PANEL_BG, padx=14, pady=12)
        if managed:
            card.pack(fill=tk.BOTH, expand=True)
        card.bind("<Enter>", lambda _event: self._set_hover_stack("news"))
        card.bind("<Leave>", lambda _event: self._clear_hover_stack("news"))
        card.bind("<MouseWheel>", lambda event: self._wheel_news_stack(event))
        top = tk.Frame(card, bg=PANEL_BG)
        top.pack(fill=tk.X)
        tk.Label(top, text="●", bg=PANEL_BG, fg=heat_color, font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT)
        tk.Label(top, text=f"  {card_data.get('title', '周末机会')}", bg=PANEL_BG, fg=TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(side=tk.LEFT)
        tk.Label(top, text=f"热度 {heat:.0%}", bg=SOFT_BLUE, fg=BLUE, font=("Microsoft YaHei UI", 8, "bold"), padx=8, pady=2).pack(side=tk.RIGHT)
        tk.Label(card, text=f"板块  {card_data.get('sector', '-')}", bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 9, "bold"), anchor="w").pack(fill=tk.X, pady=(12, 0))
        tk.Label(card, text=_short(card_data.get("logic"), 150), bg=PANEL_BG, fg=TEXT, font=("Microsoft YaHei UI", 9), justify=tk.LEFT, anchor="w", wraplength=430).pack(fill=tk.X, pady=(8, 0))
        leaders = card_data.get("leaders") or []
        leader_text = "  ".join(
            f"{leader.get('name') or leader.get('symbol')}({leader.get('symbol', '-')})"
            for leader in leaders[:4]
        ) or "-"
        tk.Label(card, text=f"龙头  {leader_text}", bg=PANEL_BG, fg=BLUE, font=("Microsoft YaHei UI", 9, "bold"), justify=tk.LEFT, anchor="w", wraplength=430).pack(fill=tk.X, pady=(12, 0))
        tk.Label(card, text=f"风险  {_short(card_data.get('risk_note'), 130) or '-'}", bg=PANEL_BG, fg=DOWN_FG, font=("Microsoft YaHei UI", 8), justify=tk.LEFT, anchor="w", wraplength=430).pack(fill=tk.X, pady=(10, 0))
        for widget in card.winfo_children():
            widget.bind("<Enter>", lambda _event: self._set_hover_stack("news"))
            widget.bind("<MouseWheel>", lambda event: self._wheel_news_stack(event))
        return card

    def _open_analysis_dialog(self, symbol: str) -> None:
        row = next((item for item in self.current_rows if item.get("symbol") == symbol), None)
        if not row:
            return
        existing = self.dialogs.get(symbol)
        if existing and existing.winfo_exists():
            existing.lift()
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"{row.get('name', symbol)} {symbol} 最新委员会分析")
        dialog.geometry("620x560")
        dialog.minsize(520, 420)
        dialog.configure(bg=BOARD_BG)
        dialog.overrideredirect(True)
        self.dialogs[symbol] = dialog
        dialog.protocol("WM_DELETE_WINDOW", lambda s=symbol, d=dialog: self._close_dialog(s, d))

        titlebar = tk.Frame(dialog, bg=PANEL_BG, height=38)
        titlebar.pack(fill=tk.X)
        titlebar.pack_propagate(False)
        title = tk.Label(
            titlebar,
            text=f"{row.get('name', symbol)}  {symbol}",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=14,
        )
        title.pack(side=tk.LEFT, fill=tk.Y)
        status_label = tk.Label(
            titlebar,
            text="正在运行最新委员会分析...",
            bg=PANEL_BG,
            fg=BLUE,
            font=("Microsoft YaHei UI", 9),
        )
        status_label.pack(side=tk.LEFT, padx=(8, 0))
        close_btn = tk.Label(
            titlebar,
            text="×",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI", 14),
            width=4,
            cursor="hand2",
        )
        close_btn.pack(side=tk.RIGHT, fill=tk.Y)
        close_btn.bind("<Button-1>", lambda _event, s=symbol, d=dialog: self._close_dialog(s, d))
        close_btn.bind("<Enter>", lambda _event: close_btn.configure(bg="#e5484d", fg="#ffffff"))
        close_btn.bind("<Leave>", lambda _event: close_btn.configure(bg=PANEL_BG, fg=MUTED))

        drag_origin: Dict[str, int] = {}

        def start_drag(event: tk.Event) -> None:
            drag_origin["x"] = event.x_root - dialog.winfo_x()
            drag_origin["y"] = event.y_root - dialog.winfo_y()

        def drag_window(event: tk.Event) -> None:
            if "x" not in drag_origin:
                return
            dialog.geometry(f"+{event.x_root - drag_origin['x']}+{event.y_root - drag_origin['y']}")

        for widget in (titlebar, title, status_label):
            widget.bind("<ButtonPress-1>", start_drag)
            widget.bind("<B1-Motion>", drag_window)

        body = tk.Frame(dialog, bg=BOARD_BG, padx=14, pady=12)
        body.pack(fill=tk.BOTH, expand=True)

        op = row.get("operation") or {}
        price = row.get("price") or {}
        fundamental = row.get("fundamental") or {}
        summary = tk.Frame(body, bg=_card_bg(row), padx=12, pady=10)
        summary.pack(fill=tk.X)
        summary_top = tk.Frame(summary, bg=_card_bg(row))
        summary_top.pack(fill=tk.X)
        tk.Label(summary_top, text="●", bg=_card_bg(row), fg=_state_color(row), font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT)
        tk.Label(
            summary_top,
            text=f"  {_label_state(row.get('state'))}",
            bg=_card_bg(row),
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            summary_top,
            text=_operation_summary(row),
            bg=SOFT_BLUE if _row_tag(row) != "action" else "#ffe5e0",
            fg=ACTION_FG if _row_tag(row) == "action" else BLUE,
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=8,
            pady=2,
        ).pack(side=tk.RIGHT)

        summary_mid = tk.Frame(summary, bg=_card_bg(row))
        summary_mid.pack(fill=tk.X, pady=(8, 0))
        tk.Label(summary_mid, text=_fmt_price(price.get("current")), bg=_card_bg(row), fg=TEXT, font=("Microsoft YaHei UI", 18, "bold")).pack(side=tk.LEFT)
        tk.Label(summary_mid, text=_fmt_pct(price.get("change_pct")), bg=_card_bg(row), fg=_change_color(row), font=("Microsoft YaHei UI", 11, "bold")).pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(summary_mid, text=f"{_verdict_signal(op.get('verdict'))} {op.get('verdict', '-')}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 10)).pack(side=tk.RIGHT)

        summary_bottom = tk.Frame(summary, bg=_card_bg(row))
        summary_bottom.pack(fill=tk.X, pady=(8, 0))
        tk.Label(summary_bottom, text=f"买 {_buy_summary(row)}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        tk.Label(summary_bottom, text=f"卖 {_exit_summary(row)}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=(12, 0))
        tk.Label(summary_bottom, text=f"基 {float(_safe_num(fundamental.get('score'), 50)):.0f}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT)

        text = tk.Text(
            body,
            wrap=tk.WORD,
            font=("Microsoft YaHei UI", 10),
            padx=12,
            pady=12,
            bg=PANEL_BG,
            fg=TEXT,
            relief=tk.FLAT,
            bd=0,
            highlightthickness=0,
            insertbackground=TEXT,
        )
        text.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        text.insert(tk.END, _snapshot_detail(row))
        text.insert(tk.END, "\n\n===== 后台任务 =====\n正在获取最新价格并调用委员会，请稍候...\n")
        text.configure(state=tk.DISABLED)
        dialog._analysis_text = text  # type: ignore[attr-defined]
        dialog._analysis_status = status_label  # type: ignore[attr-defined]

        threading.Thread(
            target=self._analysis_worker,
            args=(symbol, row),
            daemon=True,
        ).start()

    def _close_dialog(self, symbol: str, dialog: tk.Toplevel) -> None:
        self.dialogs.pop(symbol, None)
        dialog.destroy()

    def _analysis_worker(self, symbol: str, row: Dict[str, Any]) -> None:
        try:
            result = _run_latest_committee_for_row(row)
            self.analysis_queue.put((symbol, "done", result))
        except Exception as exc:  # noqa: BLE001
            self.analysis_queue.put((symbol, "error", {"success": False, "symbol": symbol, "error": str(exc)}))

    def _poll_analysis_queue(self) -> None:
        if self.closing:
            return
        while True:
            try:
                symbol, _status, result = self.analysis_queue.get_nowait()
            except queue.Empty:
                break
            dialog = self.dialogs.get(symbol)
            if dialog and dialog.winfo_exists() and result is not None:
                text = getattr(dialog, "_analysis_text", None)
                status_label = getattr(dialog, "_analysis_status", None)
                if text is not None:
                    row = next((item for item in self.current_rows if item.get("symbol") == symbol), {})
                    text.configure(state=tk.NORMAL)
                    text.insert(tk.END, _format_committee_result(row, result))
                    text.configure(state=tk.DISABLED)
                    text.see(tk.END)
                    if status_label is not None:
                        if result.get("success"):
                            status_label.configure(text="委员会分析已完成", fg=UP_FG)
                        else:
                            status_label.configure(text="委员会分析失败", fg=DOWN_FG)
                    dialog.lift()
        self.root.after(250, self._poll_analysis_queue)

    def _maybe_alert(self, rows: List[Dict[str, Any]]) -> None:
        symbols = {
            str(row.get("symbol"))
            for row in rows
            if row.get("state") == "action_required" and row.get("symbol")
        }
        new_symbols = symbols - self.previous_action_symbols
        self.previous_action_symbols = symbols
        if new_symbols and self.shake_enabled:
            self._shake()
            self._beep()

    def _shake(self) -> None:
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        offsets = [0, 12, -12, 10, -10, 6, -6, 0]

        def step(i: int = 0) -> None:
            if i >= len(offsets):
                self.root.geometry(f"+{x}+{y}")
                return
            self.root.geometry(f"+{x + offsets[i]}+{y}")
            self.root.after(45, lambda: step(i + 1))

        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(800, lambda: self.root.attributes("-topmost", False))
        step()

    def _beep(self) -> None:
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            self.root.bell()


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenInvest standalone monitor window")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--weekend-news-dir", type=Path, default=WEEKEND_NEWS_DIR)
    parser.add_argument("--poll-ms", type=int, default=1000)
    parser.add_argument("--no-shake", action="store_true")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    MonitorWindow(
        snapshot_path=args.snapshot,
        poll_ms=args.poll_ms,
        shake_enabled=not args.no_shake,
        weekend_news_dir=args.weekend_news_dir,
        demo=args.demo,
    ).start()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    main()
