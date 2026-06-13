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
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "data" / "market_monitor" / "latest_window.json"
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


def _safe_num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_price(value: Any) -> str:
    value = _safe_num(value)
    return f"{value:.2f}" if value > 0 else "-"


def _fmt_money(value: Any) -> str:
    value = _safe_num(value)
    return f"{value:,.0f}" if abs(value) >= 1 else "-"


def _fmt_lots(value: float) -> str:
    if abs(value) >= 1:
        return f"{math.floor(abs(value))}手"
    return "-"


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
    if not path.exists():
        return {
            "generated_at": None,
            "round_time": "",
            "counts": {"symbols": 0, "action_required": 0, "entry_exit_alerts": 0, "errors": 0},
            "rows": [],
            "message": f"暂无监控快照: {path}",
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "generated_at": None,
            "round_time": "",
            "counts": {"symbols": 0, "action_required": 0, "entry_exit_alerts": 0, "errors": 1},
            "rows": [],
            "message": f"读取监控快照失败: {type(exc).__name__}: {exc}",
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


class MonitorWindow:
    def __init__(self, snapshot_path: Path, refresh_ms: int, *, shake_enabled: bool = True) -> None:
        self.snapshot_path = snapshot_path
        self.refresh_ms = max(1000, refresh_ms)
        self.shake_enabled = shake_enabled
        self.root = tk.Tk()
        self.root.title("OpenInvest 监控窗口")
        self.root.overrideredirect(True)
        self._set_initial_geometry()
        self.root.configure(bg=BOARD_BG)
        self.drag_origin: Optional[tuple[int, int]] = None
        self.pinned = False
        self.previous_action_symbols: set[str] = set()
        self.current_rows: List[Dict[str, Any]] = []
        self.filter_text = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="等待监控快照")
        self.card_widgets: Dict[str, tk.Frame] = {}
        self.dialogs: Dict[str, tk.Toplevel] = {}
        self.analysis_queue: queue.Queue[tuple[str, str, Optional[Dict[str, Any]]]] = queue.Queue()
        self._build_ui()

    def _set_initial_geometry(self) -> None:
        screen_w = self.root.winfo_screenwidth()
        width = min(max(330, screen_w // 4), 420)
        height = 360
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

        titlebar = tk.Frame(self.root, bg=PANEL_BG, height=36)
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
            text="实时监控",
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
        close_btn.bind("<Button-1>", lambda _event: self.root.destroy())
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

        top = tk.Frame(self.root, bg=BOARD_BG, padx=14, pady=10)
        top.pack(fill=tk.X, pady=(2, 0))

        search_wrap = tk.Frame(self.root, bg="#eef2f7", padx=12, pady=8)
        search_wrap.pack(fill=tk.X, padx=14, pady=(4, 10))
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

        section = tk.Frame(self.root, bg=BOARD_BG, padx=16)
        section.pack(fill=tk.X)
        tk.Label(section, text="标的监控", bg=BOARD_BG, fg=MUTED, font=("Microsoft YaHei UI", 9, "bold")).pack(side=tk.LEFT)
        tk.Label(section, textvariable=self.status_text, bg=BOARD_BG, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT)
        self.filter_text.trace_add("write", lambda *_: self._render_rows())

        self.canvas = tk.Canvas(self.root, bg=BOARD_BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=14, pady=(8, 18))
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
        self.refresh()
        self._poll_analysis_queue()
        self.root.mainloop()

    def refresh(self) -> None:
        payload = _load_snapshot(self.snapshot_path)
        rows = list(payload.get("rows") or [])
        self.current_rows = rows
        counts = payload.get("counts") or {}
        self.status_text.set(
            f"{counts.get('symbols', len(rows))} 标的 / {counts.get('action_required', 0)} 操作"
        )
        self._render_rows()
        self._resize_to_rows(len(self._filtered_rows()))
        self._place_refresh_fab()
        self._maybe_alert(rows)
        self.root.after(self.refresh_ms, self.refresh)

    def _resize_to_rows(self, row_count: int) -> None:
        screen_h = self.root.winfo_screenheight()
        screen_w = self.root.winfo_screenwidth()
        visible_rows = min(max(row_count, 1), 5)
        height = 214 + visible_rows * 112
        height = min(max(height, 260), max(300, screen_h - 120))
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
        if self.root.focus_get() is None:
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

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

    def _render_rows(self) -> None:
        for child in self.cards_frame.winfo_children():
            child.destroy()
        self.card_widgets.clear()
        for row in self._filtered_rows():
            self._create_card(row)
        if not self.cards_frame.winfo_children():
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

    def _create_card(self, row: Dict[str, Any]) -> None:
        symbol = str(row.get("symbol") or "")
        op = row.get("operation") or {}
        price = row.get("price") or {}
        fundamental = row.get("fundamental") or {}
        bg = _card_bg(row)
        state_color = _state_color(row)
        change_color = _change_color(row)

        card = tk.Frame(self.cards_frame, bg=bg, padx=10, pady=8, cursor="hand2")
        card.pack(fill=tk.X, pady=(0, 8))
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
        trade_bar.pack(fill=tk.X, pady=(8, 0))
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
        lots_entry.pack(side=tk.LEFT, padx=(4, 8), ipady=3)
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
        mid.pack(fill=tk.X, pady=(8, 0))
        tk.Label(mid, text=_fmt_price(price.get("current")), bg=bg, fg=TEXT, font=("Microsoft YaHei UI", 14, "bold")).pack(side=tk.LEFT)
        tk.Label(mid, text=_fmt_pct(price.get("change_pct")), bg=bg, fg=change_color, font=("Microsoft YaHei UI", 10, "bold")).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(mid, text=f"{_verdict_signal(op.get('verdict'))} {op.get('verdict', '-')}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side=tk.RIGHT)

        bottom = tk.Frame(card, bg=bg)
        bottom.pack(fill=tk.X, pady=(6, 0))
        tk.Label(bottom, text=f"买 {_buy_summary(row)}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        tk.Label(bottom, text=f"卖 {_exit_summary(row)}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(bottom, text=f"基 {float(_safe_num(fundamental.get('score'), 50)):.0f}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT)

        for widget in (card, top, mid, bottom):
            widget.bind("<Button-1>", lambda _event, s=symbol: self._open_analysis_dialog(s))
        for widget in card.winfo_children():
            if widget is trade_bar:
                continue
            widget.bind("<Button-1>", lambda _event, s=symbol: self._open_analysis_dialog(s))
            for nested in widget.winfo_children():
                nested.bind("<Button-1>", lambda _event, s=symbol: self._open_analysis_dialog(s))

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
    parser.add_argument("--refresh-sec", type=float, default=5.0)
    parser.add_argument("--no-shake", action="store_true")
    args = parser.parse_args()
    MonitorWindow(
        snapshot_path=args.snapshot,
        refresh_ms=int(args.refresh_sec * 1000),
        shake_enabled=not args.no_shake,
    ).start()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    main()
