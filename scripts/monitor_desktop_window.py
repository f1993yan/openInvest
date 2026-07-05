"""Standalone desktop monitor window for intraday target status.

The market monitor writes ``data/market_monitor/latest_window.json`` after each
round. This Tkinter window reads that snapshot, refreshes in place, and shakes
when a new action-required state appears. Double-clicking a row opens a dialog
and runs the latest committee analysis for that symbol in a background thread.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv


_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))

from scripts.monitor_window_constants import *
from scripts.monitor_window_services import *
from scripts.monitor_window_text import *
from scripts.monitor_window_widgets import *
from scripts.monitor_window_analysis import MonitorAnalysisMixin
from scripts.monitor_window_news import MonitorNewsMixin
from scripts.monitor_window_selection import MonitorSelectionMixin
from scripts.monitor_window_trade import MonitorTradeMixin


def _account_ledger_db_path() -> Path:
    from db.account_ledger import DB_PATH

    return Path(DB_PATH)


def _config_has_targets(config: Dict[str, Any]) -> bool:
    return bool((config.get("holdings") or []) or (config.get("watchlist") or []))


class MonitorWindow(MonitorNewsMixin, MonitorSelectionMixin, MonitorTradeMixin, MonitorAnalysisMixin):
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
        self.news_scroll_canvas: Optional[tk.Canvas] = None
        self.news_pager_overlay: Optional[tk.Frame] = None
        self.news_cards: List[Dict[str, Any]] = []
        self.news_source: str = ""
        self.trade_popover: Optional[tk.Frame] = None
        self.cash_popover: Optional[tk.Frame] = None
        self.trade_scroll_canvas: Optional[tk.Canvas] = None
        self.selection_popover: Optional[tk.Frame] = None
        self.selection_popover_symbol: Optional[str] = None
        self.selection_scroll_canvas: Optional[tk.Canvas] = None
        self.selection_buttons_canvas: Optional[tk.Canvas] = None
        self.selection_buttons_window: Optional[int] = None
        self.filter_text = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="等待监控快照")
        self.last_update_text = tk.StringVar(value="更新 -")
        self.trading_mode_text = tk.StringVar(value="主动盈利")
        self.trading_mode_switch: Optional[ModeSlider] = None
        self.action_email_enabled = True
        self.action_email_btn: Optional[tk.Label] = None
        self.card_widgets: Dict[str, tk.Frame] = {}
        self.dialogs: Dict[str, tk.Toplevel] = {}
        self.analysis_queue: queue.Queue[tuple[str, str, Optional[Dict[str, Any]]]] = queue.Queue()
        load_dotenv(ROOT / ".env")
        self.remote_server_url = os.getenv("INVEST_REMOTE_SERVER_URL")
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
        style.configure(
            "App.Vertical.TScrollbar",
            gripcount=0,
            background="#c9d6e6",
            darkcolor="#c9d6e6",
            lightcolor="#c9d6e6",
            troughcolor="#eef2f7",
            bordercolor="#eef2f7",
            arrowcolor="#c9d6e6",
            width=8,
            relief=tk.FLAT,
        )
        style.configure(
            "Analysis.Horizontal.TProgressbar",
            troughcolor=LINE,
            background=BLUE,
            bordercolor=PANEL_BG,
            lightcolor=BLUE,
            darkcolor=BLUE,
        )

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
        self.action_email_btn = tk.Label(
            titlebar,
            text="邮件开",
            bg="#eef6ff",
            fg=BLUE,
            font=("Microsoft YaHei UI", 8, "bold"),
            padx=8,
            cursor="hand2",
        )
        self.action_email_btn.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 2), pady=5)
        close_btn.bind("<Button-1>", lambda _event: self._on_close())
        min_btn.bind("<Button-1>", lambda _event: self._minimize())
        self.pin_btn.bind("<Button-1>", lambda _event: self._toggle_pin())
        self.action_email_btn.bind("<Button-1>", lambda _event: self._toggle_action_email_enabled())
        close_btn.bind("<Enter>", lambda _event: close_btn.configure(bg="#e5484d", fg="#ffffff"))
        close_btn.bind("<Leave>", lambda _event: close_btn.configure(bg=PANEL_BG, fg=MUTED))
        min_btn.bind("<Enter>", lambda _event: min_btn.configure(bg="#eef2f7", fg=TEXT))
        min_btn.bind("<Leave>", lambda _event: min_btn.configure(bg=PANEL_BG, fg=MUTED))
        self.pin_btn.bind("<Enter>", lambda _event: self.pin_btn.configure(bg="#eef2f7"))
        self.pin_btn.bind("<Leave>", lambda _event: self.pin_btn.configure(bg=PANEL_BG))
        self.action_email_btn.bind("<Enter>", lambda _event: self._render_action_email_button(self.action_email_enabled, hover=True))
        self.action_email_btn.bind("<Leave>", lambda _event: self._render_action_email_button(self.action_email_enabled))
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
        self.cash_bar = CashRatioBar(top, width=178, height=18)
        self.cash_bar.pack(side=tk.RIGHT)
        self.cash_bar.configure(cursor="hand2")
        self.cash_bar.bind("<Button-1>", lambda _event: self._toggle_cash_correction_popover())
        self.trading_mode_switch = ModeSlider(
            top,
            options=(("active_profit", "盈利"), ("cash_recovery", "回收"), ("risk_off", "避险")),
            command=self._change_trading_mode,
            width=92,
            height=22,
        )
        self.trading_mode_switch.pack(side=tk.RIGHT, padx=(0, 8))

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
        search.bind("<Return>", lambda event: self._on_search_return(event))

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

        # Pack FABs on the right of selection_bar for horizontal bottom-alignment
        self.refresh_fab = CircleButton(
            self.selection_bar,
            text="↻",
            command=self.refresh,
            size=32,
            font=("Segoe UI Symbol", 12, "bold"),
        )
        self.refresh_fab.pack(side=tk.RIGHT, padx=(6, 0))

        self.upload_fab = CircleButton(
            self.selection_bar,
            text="↑",
            command=self.upload_data,
            size=32,
            color="#10b981",
            hover_color="#059669",
            font=("Segoe UI", 12, "bold"),
        )
        self.upload_fab.pack(side=tk.RIGHT, padx=(6, 0))

        self.sync_fab = CircleButton(
            self.selection_bar,
            text="↓",
            command=self.sync_data,
            size=32,
            color="#f59e0b",
            hover_color="#d97706",
            font=("Segoe UI", 12, "bold"),
        )
        self.sync_fab.pack(side=tk.RIGHT, padx=(6, 0))

        self.trade_fab = CircleButton(
            self.selection_bar,
            text="+",
            command=self._toggle_trade_popover,
            size=32,
            color="#111827",
            hover_color="#374151",
            font=("Segoe UI", 14, "bold"),
        )
        self.trade_fab.pack(side=tk.RIGHT, padx=(6, 0))

        self.selection_buttons_canvas = tk.Canvas(
            self.selection_bar,
            bg=BOARD_BG,
            height=32,
            highlightthickness=0,
            bd=0,
        )
        self.selection_buttons_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.selection_buttons_frame = tk.Frame(self.selection_buttons_canvas, bg=BOARD_BG)
        self.selection_buttons_window = self.selection_buttons_canvas.create_window(
            (0, 0),
            window=self.selection_buttons_frame,
            anchor="nw",
        )
        self.selection_buttons_frame.bind("<Configure>", self._on_selection_buttons_configure)
        self.selection_bar.bind("<MouseWheel>", self._wheel_selection_bar)
        self.selection_buttons_frame.bind("<MouseWheel>", self._wheel_selection_bar)
        self.selection_buttons_canvas.bind("<MouseWheel>", self._wheel_selection_bar)

        self.canvas = tk.Canvas(self.root, bg=BOARD_BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 6))
        self.cards_frame = tk.Frame(self.canvas, bg=BOARD_BG)
        self.cards_window = self.canvas.create_window((0, 0), window=self.cards_frame, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.cards_frame.bind("<Configure>", self._on_cards_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

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
        self.root.configure(bg=BOARD_BG)
        payload = (
            {
                "rows": _demo_rows(),
                "counts": {"symbols": 2, "action_required": 1},
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "cash_cny": 28600,
                "available_cash_cny": 28600,
                "t2_pending_cash_cny": 5200,
                "total_cash_cny": 33800,
                "total_assets_cny": 100000,
                "trading_mode": {"mode": "active_profit", "label": "主动盈利"},
                "monitor_action_email_enabled": True,
            }
            if self.demo
            else _load_snapshot(self.snapshot_path)
        )
        mode_payload = payload.get("trading_mode") if self.demo else _current_trading_mode()
        mode_payload = mode_payload or _current_trading_mode()
        if not self.demo:
            payload["trading_mode"] = mode_payload
        email_enabled = bool(payload.get("monitor_action_email_enabled", True)) if self.demo else _current_action_email_enabled()
        payload["monitor_action_email_enabled"] = email_enabled
        mode_key = str((mode_payload or {}).get("mode") or "active_profit")
        mode_label = str((mode_payload or {}).get("label") or "主动盈利")
        rows = _sort_stock_rows(
            _fill_missing_row_sectors(list(payload.get("rows") or []))
            if self.demo
            else _fill_missing_row_sectors(_filter_rows_by_current_config(list(payload.get("rows") or [])))
        )
        stock_signature = tuple(
            (
                row.get("symbol"),
                row.get("name"),
                row.get("sector"),
                row.get("industry"),
                row.get("state"),
                (row.get("operation") or {}).get("verdict"),
                _safe_num((row.get("operation") or {}).get("suggested_alloc_cny")),
                _safe_num((row.get("operation") or {}).get("optimizer_lots")),
                _safe_num((row.get("operation") or {}).get("llm_review_lots")),
                (row.get("operation") or {}).get("llm_position_scale"),
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
            _safe_num(payload.get("available_cash_cny")),
            _safe_num(payload.get("t2_pending_cash_cny")),
            _safe_num(payload.get("total_cash_cny")),
            _safe_num(payload.get("total_assets_cny")),
            mode_key,
            email_enabled,
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
        elif payload.get("generated_at") != (self.last_payload_signature[0] if self.last_payload_signature else None):
            self.stock_page_index = 0
        self.stock_content_signature = stock_signature
        self.last_payload_signature = payload_signature
        self.current_rows = rows
        self.status_text.set(
            f"{counts.get('symbols', len(rows))} 标的 / {counts.get('action_required', 0)} 操作"
        )
        self.last_update_text.set(_fmt_update_time(payload.get("generated_at")))
        self.trading_mode_text.set(mode_label)
        self._render_trading_mode_buttons(mode_key)
        self._render_action_email_button(email_enabled)
        self.cash_bar.set_values(
            payload.get("available_cash_cny", payload.get("cash_cny")),
            payload.get("total_assets_cny"),
            payload.get("t2_pending_cash_cny", 0),
        )
        self._render_rows()
        self._resize_to_rows(len(self._filtered_rows()))
        self._place_refresh_fab()
        self._maybe_alert(rows)
        self._refresh_open_news_popover()
        self._render_selection_buttons()

    def _render_trading_mode_buttons(self, active_mode: str) -> None:
        if self.trading_mode_switch is not None:
            self.trading_mode_switch.set_active(active_mode)

    def _render_action_email_button(self, enabled: bool, *, hover: bool = False) -> None:
        self.action_email_enabled = bool(enabled)
        if self.action_email_btn is None:
            return
        if enabled:
            bg = "#dbeafe" if hover else "#eef6ff"
            fg = BLUE
            text = "邮件开"
        else:
            bg = "#f2f4f7" if hover else "#f8fafc"
            fg = MUTED
            text = "邮件关"
        self.action_email_btn.configure(text=text, bg=bg, fg=fg)

    def _toggle_action_email_enabled(self) -> None:
        next_enabled = not self.action_email_enabled
        try:
            if not self.demo:
                next_enabled = _set_action_email_enabled(next_enabled)
            self._render_action_email_button(next_enabled)
            self.status_text.set(f"执行邮件: {'开' if next_enabled else '关'}")
            self.last_payload_signature = ()
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"切换执行邮件失败: {exc}")

    def _change_trading_mode(self, mode: str) -> None:
        try:
            if self.demo:
                payload = {"mode": mode, "label": {"active_profit": "主动盈利", "cash_recovery": "现金回收", "risk_off": "主动避险"}.get(mode, "主动盈利")}
            else:
                payload = _set_trading_mode(mode)
            self.trading_mode_text.set(payload.get("label", "主动盈利"))
            self._render_trading_mode_buttons(payload.get("mode", "active_profit"))
            self.status_text.set(f"交易模式: {payload.get('label', '主动盈利')}")
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"切换交易模式失败: {exc}")

    def _apply_committee_row_update(self, row: Dict[str, Any]) -> None:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            return
        if self.demo:
            rows = []
            replaced = False
            for item in self.current_rows:
                if str(item.get("symbol") or "").upper() == symbol:
                    rows.append(row)
                    replaced = True
                else:
                    rows.append(item)
            if not replaced:
                rows.append(row)
            self.current_rows = _sort_stock_rows(rows)
            self.stock_page_index = 0
            self._render_rows()
            self._resize_to_rows(len(self._filtered_rows()))
            self.status_text.set(
                f"{len(self.current_rows)} 标的 / {sum(1 for item in self.current_rows if item.get('state') == 'action_required')} 操作"
            )
            self.last_update_text.set(_fmt_update_time(datetime.now().isoformat(timespec="seconds")))
            self._maybe_alert(self.current_rows)
            return

        _update_snapshot_row(self.snapshot_path, row)
        self.watched_mtime_signature = self._watched_file_signature()
        self.last_payload_signature = ()
        self.refresh()

    def upload_data(self) -> None:
        if not self.remote_server_url:
            messagebox.showinfo("跳过上传", "未配置远程服务器 (INVEST_REMOTE_SERVER_URL)。\n本地配置文件已由监控服务自动保存。")
            return

        config_path = ROOT / "jobs" / "market_monitor_config.json"
        exit_path = ROOT / "reports" / "weekly_exit_param_optimization.json"

        if not config_path.exists():
            messagebox.showerror("错误", f"配置文件不存在: {config_path}")
            return

        import requests
        try:
            try:
                from db.account_ledger import AccountLedger

                ledger = AccountLedger()
                ledger.sync_to_config_and_snapshot()
                try:
                    ledger.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    ledger.conn.commit()
                except Exception as checkpoint_err:
                    print(f"Failed to checkpoint account ledger before upload: {checkpoint_err}")
            except Exception as ledger_err:
                print(f"Failed to refresh config from ledger before upload: {ledger_err}")

            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)

            url = f"{self.remote_server_url.rstrip('/')}/api/config/monitor_config"
            try:
                resp = requests.post(url, json=config_data, timeout=60)
            except requests.exceptions.ConnectionError:
                messagebox.showwarning("连接失败", f"无法连接到服务器 {url}。\n本地数据已自动保存，无需上传。")
                return
            if resp.status_code != 200:
                messagebox.showerror("错误", f"上传监控配置失败: HTTP {resp.status_code}\n{resp.text}")
                return

            if exit_path.exists():
                with open(exit_path, "r", encoding="utf-8") as f:
                    exit_data = json.load(f)

                # Inject sector policies from environment variables
                sector_policies = os.getenv("INVEST_A_SHARE_SECTOR_EXIT_POLICIES", "")
                if sector_policies:
                    try:
                        exit_data["sector_exit_policies"] = json.loads(sector_policies)
                    except Exception:
                        exit_data["sector_exit_policies"] = sector_policies
                    try:
                        with open(exit_path, "w", encoding="utf-8") as f:
                            json.dump(exit_data, f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass

                url_exit = f"{self.remote_server_url.rstrip('/')}/api/config/exit_params"
                resp_exit = requests.post(url_exit, json=exit_data, timeout=10)
                if resp_exit.status_code != 200:
                    messagebox.showerror("错误", f"上传每周止盈参数失败: HTTP {resp_exit.status_code}\n{resp_exit.text}")
                    return

            # 1. 上传 .env 的 INVEST_A_SHARE_SECTOR_EXIT_POLICIES
            sector_policies = os.getenv("INVEST_A_SHARE_SECTOR_EXIT_POLICIES", "")
            if sector_policies:
                url_env = f"{self.remote_server_url.rstrip('/')}/api/config/env_policies"
                resp_env = requests.post(url_env, json={"policies": sector_policies}, timeout=10)
                if resp_env.status_code != 200:
                    print(f"Failed to upload env policies to server: {resp_env.status_code}")

            # 2. 上传 data/sector_cache.json
            sector_cache_path = ROOT / "data" / "sector_cache.json"
            if sector_cache_path.exists():
                try:
                    with open(sector_cache_path, "r", encoding="utf-8") as f:
                        sector_data = json.load(f)
                    url_sector = f"{self.remote_server_url.rstrip('/')}/api/config/sector_cache"
                    resp_sector = requests.post(url_sector, json=sector_data, timeout=10)
                    if resp_sector.status_code != 200:
                        print(f"Failed to upload sector cache to server: {resp_sector.status_code}")
                except Exception as se_err:
                    print(f"Failed to read/upload sector cache: {se_err}")

            # 3. 上传真实双账户账本 db/accounts.db
            db_path = _account_ledger_db_path()
            if db_path.exists():
                try:
                    url_ledger = f"{self.remote_server_url.rstrip('/')}/api/config/account_ledger"
                    with open(db_path, "rb") as f:
                        resp_ledger = requests.post(url_ledger, files={"file": f}, timeout=15)
                    if resp_ledger.status_code != 200:
                        print(f"Failed to upload account ledger to server: {resp_ledger.status_code}")
                except Exception as db_err:
                    print(f"Failed to read/upload account ledger: {db_err}")

            messagebox.showinfo("成功", "所有配置文件及账本数据库上传成功！")
        except Exception as e:
            messagebox.showerror("错误", f"上传过程中发生异常: {str(e)}")

    def sync_data(self) -> None:
        if not self.remote_server_url:
            messagebox.showinfo("跳过同步", "未配置远程服务器 (INVEST_REMOTE_SERVER_URL)。\n本地数据无需同步。")
            return

        if not messagebox.askyesno("确认同步", "确认从服务器同步持仓/关注/参数数据及账本数据库？这将覆盖本地的相同数据！"):
            return

        import requests
        try:
            config_path = ROOT / "jobs" / "market_monitor_config.json"
            config_received = False
            url_config = f"{self.remote_server_url.rstrip('/')}/api/config/monitor_config"
            resp_config = requests.get(url_config, timeout=10)
            config_data: Dict[str, Any] = {}
            if resp_config.status_code == 200:
                config_received = True
                config_data = resp_config.json()
                if _config_has_targets(config_data):
                    config_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(config_path, "w", encoding="utf-8") as f:
                        json.dump(config_data, f, ensure_ascii=False, indent=2)
            elif resp_config.status_code == 404:
                pass
            else:
                messagebox.showerror("错误", f"同步监控配置失败: HTTP {resp_config.status_code}\n{resp_config.text}")
                return

            url_exit = f"{self.remote_server_url.rstrip('/')}/api/config/exit_params"
            resp_exit = requests.get(url_exit, timeout=10)
            if resp_exit.status_code == 200:
                exit_data = resp_exit.json()
                exit_path = ROOT / "reports" / "weekly_exit_param_optimization.json"
                exit_path.parent.mkdir(parents=True, exist_ok=True)
                with open(exit_path, "w", encoding="utf-8") as f:
                    json.dump(exit_data, f, ensure_ascii=False, indent=2)
            elif resp_exit.status_code == 404:
                pass
            else:
                messagebox.showerror("错误", f"同步每周止盈参数失败: HTTP {resp_exit.status_code}\n{resp_exit.text}")
                return

            # 1. 同步 .env 中的 INVEST_A_SHARE_SECTOR_EXIT_POLICIES
            try:
                url_env = f"{self.remote_server_url.rstrip('/')}/api/config/env_policies"
                resp_env = requests.get(url_env, timeout=10)
                if resp_env.status_code == 200:
                    policies_val = resp_env.json().get("policies", "")
                    if policies_val:
                        env_path = ROOT / ".env"
                        content = ""
                        if env_path.exists():
                            with open(env_path, "r", encoding="utf-8") as f:
                                content = f.read()
                        lines = content.splitlines()
                        found = False
                        key = "INVEST_A_SHARE_SECTOR_EXIT_POLICIES"
                        new_line = f"{key}={policies_val}"
                        for i, line in enumerate(lines):
                            if line.strip().startswith(f"{key}="):
                                lines[i] = new_line
                                found = True
                                break
                        if not found:
                            lines.append(new_line)
                        with open(env_path, "w", encoding="utf-8") as f:
                            f.write("\n".join(lines) + "\n")
                        os.environ[key] = policies_val
            except Exception as env_err:
                print(f"Failed to sync env policies: {env_err}")

            # 2. 同步 data/sector_cache.json
            try:
                url_sector = f"{self.remote_server_url.rstrip('/')}/api/config/sector_cache"
                resp_sector = requests.get(url_sector, timeout=10)
                if resp_sector.status_code == 200:
                    sector_cache_path = ROOT / "data" / "sector_cache.json"
                    sector_cache_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(sector_cache_path, "w", encoding="utf-8") as f:
                        json.dump(resp_sector.json(), f, ensure_ascii=False, indent=2)
            except Exception as sector_err:
                print(f"Failed to sync sector cache: {sector_err}")

            # 3. 同步真实双账户账本 db/accounts.db，再由账本回写 config/snapshot
            ledger_synced = False
            try:
                url_ledger = f"{self.remote_server_url.rstrip('/')}/api/config/account_ledger"
                resp_ledger = requests.get(url_ledger, timeout=15)
                if resp_ledger.status_code == 200:
                    db_path = _account_ledger_db_path()
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
                    with open(tmp_path, "wb") as f:
                        f.write(resp_ledger.content)
                    tmp_path.replace(db_path)
                    ledger_synced = True
            except Exception as db_err:
                print(f"Failed to sync account ledger: {db_err}")

            try:
                from db.account_ledger import AccountLedger

                ledger = AccountLedger()
                if ledger_synced:
                    if config_received and not _config_has_targets(config_data) and not config_path.exists():
                        config_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(config_path, "w", encoding="utf-8") as f:
                            json.dump(config_data, f, ensure_ascii=False, indent=2)
                    ledger.sync_to_config_and_snapshot()
                elif _config_has_targets(config_data):
                    ledger.ensure_initialized(config_data)
                    ledger.sync_to_config_and_snapshot()
                elif config_data:
                    print("Remote monitor config has no targets and no ledger was synced; keeping local target list unchanged.")
            except Exception as le:
                print(f"Failed to refresh ledger/config after sync: {le}")

            messagebox.showinfo("成功", "所有配置文件及账本数据库同步成功！")
            self.refresh()
        except Exception as e:
            messagebox.showerror("错误", f"同步过程中发生异常: {str(e)}")

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
        if hasattr(self, "trade_fab"):
            self.trade_fab._draw(self.trade_fab.color)

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.cards_window, width=event.width)
        self.canvas.configure(bg=BOARD_BG)
        self._place_refresh_fab()

    def _on_cards_configure(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.configure(bg=BOARD_BG)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if (
            hasattr(self, "selection_bar")
            and self.selection_buttons_canvas is not None
            and self._widget_inside(event.widget, self.selection_bar)
        ):
            self._wheel_selection_bar(event)
            return
        if (
            self.selection_popover
            and self.selection_popover.winfo_exists()
            and self.selection_scroll_canvas is not None
            and self._widget_inside(event.widget, self.selection_popover)
        ):
            self._scroll_canvas(self.selection_scroll_canvas, event)
            return
        if (
            self.trade_popover
            and self.trade_popover.winfo_exists()
            and self.trade_scroll_canvas is not None
            and self._widget_inside(event.widget, self.trade_popover)
        ):
            self._scroll_canvas(self.trade_scroll_canvas, event)
            return
        if (
            self.news_popover
            and self.news_popover.winfo_exists()
            and self.news_scroll_canvas is not None
            and self._widget_inside(event.widget, self.news_popover)
        ):
            self._scroll_canvas(self.news_scroll_canvas, event)
            return
        if self.hover_stack != "stocks":
            return
        rows = self._filtered_rows()
        if len(rows) > 1:
            self._change_stock_page(-1 if event.delta > 0 else 1)

    def _widget_inside(self, widget: tk.Misc, ancestor: tk.Misc) -> bool:
        cursor: Optional[tk.Misc] = widget
        while cursor is not None:
            if cursor == ancestor:
                return True
            try:
                cursor = cursor.master
            except Exception:
                return False
        return False

    def _scroll_canvas(self, canvas: tk.Canvas, event: tk.Event) -> None:
        if not canvas.winfo_exists():
            return
        bbox = canvas.bbox("all")
        if not bbox:
            return
        visible_height = max(canvas.winfo_height(), 1)
        content_height = max(bbox[3] - bbox[1], 1)
        if content_height <= visible_height:
            return
        step = -1 if event.delta > 0 else 1
        canvas.yview_scroll(step, "units")

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
            self._create_stack_layers(self.cards_frame, rows, self.stock_page_index)
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

    def _create_stack_layers(self, parent: tk.Misc, rows: List[Dict[str, Any]], index: int) -> None:
        count = len(rows)
        if count <= 1:
            return
        layer_count = min(4, count - 1)
        for layer in range(layer_count, 0, -1):
            row = rows[(index + layer) % count]
            frame = tk.Frame(parent, bg=_stack_layer_bg(row), height=max(4, 8 - layer))
            frame.pack(fill=tk.X, padx=layer * 5, pady=(1 if layer == layer_count else 0, 0))
            frame.pack_propagate(False)

    def _create_neutral_stack_layers(self, parent: tk.Misc, count: int) -> None:
        if count <= 1:
            return
        for layer in range(min(4, count - 1), 0, -1):
            color = "#dce6f5" if layer % 2 else "#e7eef8"
            frame = tk.Frame(parent, bg=color, height=max(4, 8 - layer))
            frame.pack(fill=tk.X, padx=layer * 5, pady=(1 if layer == min(4, count - 1) else 0, 0))
            frame.pack_propagate(False)

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
        if _is_config_watch_only(row):
            remove_btn = tk.Label(
                top,
                text="−",
                bg="#eef2f7" if bg == CARD_BG else bg,
                fg=MUTED,
                font=("Segoe UI", 11, "bold"),
                width=2,
                cursor="hand2",
            )
            remove_btn._skip_card_bindings = True  # type: ignore[attr-defined]
            remove_btn.pack(side=tk.RIGHT, padx=(6, 0))
            remove_btn.bind("<Button-1>", lambda event, r=row: self._confirm_remove_watchlist(r, event))
            remove_btn.bind("<Double-Button-1>", lambda _event: "break")
            remove_btn.bind("<Enter>", lambda _event, w=remove_btn: w.configure(bg="#fee4e2", fg=DOWN_FG))
            remove_btn.bind("<Leave>", lambda _event, w=remove_btn: w.configure(bg="#eef2f7" if bg == CARD_BG else bg, fg=MUTED))
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
        llm_lots_hint = _llm_review_lots_hint(row)
        if llm_lots_hint:
            tk.Label(
                trade_bar,
                text=llm_lots_hint,
                bg=bg,
                fg=MUTED,
                font=("Microsoft YaHei UI", 8),
            ).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(trade_bar, textvariable=status_var, bg=bg, fg=UP_FG, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT)

        def confirm_trade(_event: Optional[tk.Event] = None, *, r: Dict[str, Any] = row) -> str:
            try:
                if r.get("_demo"):
                    raise ValueError("演示模式不会更新持仓")
                raw_lots = lots_var.get().strip()
                if not raw_lots.isdigit():
                    raise ValueError("手数必须是整数")
                lots = int(raw_lots)
                result = _execute_user_trade_from_row(r, lots)
                message = str(result.get("message") or "")
                trade = result.get("trade") or {}
                updated_row = json.loads(json.dumps(r, ensure_ascii=False))
                updated_row["state"] = "executed"
                op = dict(updated_row.get("operation") or {})
                op["status"] = "executed"
                op["execution_blocked"] = False
                op["executed_at"] = datetime.now().isoformat(timespec="seconds")
                op["executed_direction"] = trade.get("direction") or _operation_direction(r)
                op["executed_units"] = _safe_num(trade.get("units"), lots * _lot_size(r))
                op["executed_price"] = _safe_num(trade.get("price"))
                op["executed_lots"] = int(_safe_num(op.get("executed_units")) // max(1, _lot_size(r)))
                updated_row["operation"] = op
                if op["executed_direction"] == "BUY":
                    updated_row["units"] = _safe_num(updated_row.get("units")) + _safe_num(op.get("executed_units"))
                else:
                    updated_row["units"] = max(0.0, _safe_num(updated_row.get("units")) - _safe_num(op.get("executed_units")))
                if self.demo:
                    pass
                else:
                    _update_snapshot_row(self.snapshot_path, updated_row)
                    self.watched_mtime_signature = self._watched_file_signature()
                    self.last_payload_signature = ()
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
        tk.Label(mid, text=f"{_verdict_signal(op.get('verdict'))} {_compact_verdict_label(op.get('verdict'))}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side=tk.RIGHT)

        bottom_meta = tk.Frame(card, bg=bg)
        bottom_meta.pack(fill=tk.X, pady=(4, 0))
        tk.Label(bottom_meta, text=_sector_summary(row), bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        tk.Label(bottom_meta, text=f"基 {float(_safe_num(fundamental.get('score'), 50)):.0f}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT)
        tk.Label(bottom_meta, text=f"持 {_fmt_holding_lots(row)}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT, padx=(0, 10))

        bottom_levels = tk.Frame(card, bg=bg)
        bottom_levels.pack(fill=tk.X, pady=(2, 0))
        tk.Label(bottom_levels, text=f"买 {_buy_summary(row)}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        tk.Label(bottom_levels, text=f"卖 {_exit_summary(row)}", bg=bg, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=(10, 0))

        for widget in (card, top, mid, bottom_meta, bottom_levels):
            widget.bind("<Double-Button-1>", lambda _event, s=symbol: self._open_analysis_dialog(s))
        for widget in card.winfo_children():
            if widget is trade_bar:
                continue
            widget.bind("<Double-Button-1>", lambda _event, s=symbol: self._open_analysis_dialog(s))
            for nested in widget.winfo_children():
                if getattr(nested, "_skip_card_bindings", False):
                    continue
                nested.bind("<Double-Button-1>", lambda _event, s=symbol: self._open_analysis_dialog(s))
                nested.bind("<Enter>", lambda _event: self._set_hover_stack("stocks"))
                nested.bind("<MouseWheel>", lambda event: self._wheel_stock_stack(event))
        return card

    def _confirm_remove_watchlist(self, row: Dict[str, Any], event: Optional[tk.Event] = None) -> str:
        if event is not None:
            try:
                event.widget.focus_set()
            except Exception:
                pass
        symbol = str(row.get("symbol") or "").strip()
        name = str(row.get("name") or symbol)
        if not symbol:
            return "break"
        if row.get("is_holding") or _safe_num(row.get("units")) > 0:
            self.status_text.set("当前标的是持仓，不能取消关注")
            return "break"
        ok = messagebox.askyesno(
            "取消关注",
            f"确认取消关注 {name}（{symbol}）吗？\n\n这只会从关注列表移除，不会影响当前持仓。",
            parent=self.root,
        )
        if not ok:
            return "break"
        try:
            message = _remove_from_watchlist(symbol)
            self.status_text.set(message)
            self.stock_page_index = 0
            self.last_payload_signature = ()
            self.stock_content_signature = ()
            self.refresh()
        except Exception as exc:  # noqa: BLE001
            self.status_text.set(f"取消关注失败: {exc}")
            messagebox.showerror("取消关注失败", str(exc), parent=self.root)
        return "break"

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

    def _on_search_return(self, event: Optional[tk.Event] = None) -> None:
        query = self.filter_text.get().strip()
        if not query:
            return

        # Check if the query matches a symbol or name in holdings/watchlist
        query_upper = query.upper()
        match = None
        for row in self.current_rows:
            sym = str(row.get("symbol") or "").upper()
            name = str(row.get("name") or "")
            if query_upper == sym or query.strip().lower() == name.strip().lower():
                match = row
                break

        if match:
            # Found in holdings/watchlist! Just open the existing dialog
            self._open_analysis_dialog(match.get("symbol"))
        else:
            # Not in holdings/watchlist! Open dialog immediately in resolving state
            existing = self.dialogs.get(query_upper)
            if existing and existing.winfo_exists():
                existing.lift()
                return

            loading_row = {
                "symbol": query_upper,
                "name": query,
                "state": "watch",
                "operation": {"verdict": "HOLD"},
                "price": {"current": 0.0, "change_pct": 0.0},
                "fundamental": {"score": 50},
                "_is_resolving": True,
            }
            self._open_analysis_dialog(query_upper, loading_row)

    def _resolve_query_to_stock(self, query: str) -> Optional[tuple[str, str, Dict[str, Any]]]:
        query = query.strip()
        if not query:
            return None

        # Determine if it's a code
        is_code = False
        clean_code = query.upper()
        if query.isdigit() and len(query) in (5, 6):
            is_code = True
        elif (query.lower().startswith("sh") or query.lower().startswith("sz")) and query[2:].isdigit() and len(query[2:]) == 6:
            is_code = True
            clean_code = query[2:]
        elif query.lower().startswith("hk") and query[2:].isdigit() and len(query[2:]) == 5:
            is_code = True
            clean_code = query[2:]

        from utils.market_data_provider import fetch_prices, search_symbols

        if is_code:
            prices = fetch_prices([clean_code])
            if clean_code in prices:
                p_info = prices[clean_code]
                return clean_code, p_info.get("name", clean_code), p_info

            # Fallback search if price fetch failed
            found = search_symbols(clean_code, limit=1)
            if found:
                return found[0]["symbol"], found[0]["name"], {"price": 0.0, "change_pct": 0.0}
            return clean_code, clean_code, {"price": 0.0, "change_pct": 0.0}
        else:
            # Name lookup in unified provider search
            found = search_symbols(query, limit=1)
            if found:
                code = found[0]["symbol"]
                name = found[0]["name"]
                prices = fetch_prices([code])
                p_info = prices.get(code, {"price": 0.0, "change_pct": 0.0})
                return code, name, p_info

        return None

    def _toggle_cash_correction_popover(self) -> None:
        if self.cash_popover and self.cash_popover.winfo_exists():
            self._close_cash_correction_popover()
            return
        self._open_cash_correction_popover()

    def _open_cash_correction_popover(self) -> None:
        self._close_cash_correction_popover()
        if hasattr(self, "_close_trade_popover"):
            self._close_trade_popover()
        if hasattr(self, "_close_selection_popover"):
            self._close_selection_popover()

        popover = tk.Frame(self.root, bg=LINE, padx=1, pady=1)
        self.cash_popover = popover
        panel = tk.Frame(popover, bg=PANEL_BG, padx=12, pady=10)
        panel.pack(fill=tk.BOTH, expand=True)

        top = tk.Frame(panel, bg=PANEL_BG)
        top.pack(fill=tk.X)
        tk.Label(
            top,
            text="修正账户现金",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.LEFT)
        close_btn = tk.Label(top, text="×", bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 12), width=2, cursor="hand2")
        close_btn.pack(side=tk.RIGHT)
        close_btn.bind("<Button-1>", lambda _event: self._close_cash_correction_popover())

        # Available Cash field
        cash_frame = tk.Frame(panel, bg=PANEL_BG)
        cash_frame.pack(fill=tk.X, pady=(10, 5))
        tk.Label(cash_frame, text="可用现金(CNY):", bg=PANEL_BG, fg=TEXT, font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)

        curr_cash = 0.0
        curr_t2 = 0.0
        if not self.demo:
            try:
                from jobs.market_monitor import load_config
                from db.account_ledger import AccountLedger, REAL_ACCOUNT

                ledger = AccountLedger()
                ledger.ensure_initialized(load_config())
                summary = ledger.account_summary(REAL_ACCOUNT)
                curr_cash = float(summary.get("cash_cny", 0.0) or 0.0)
                curr_t2 = float(summary.get("t2_pending_cash_cny", 0.0) or 0.0)
            except Exception:
                pass

        cash_var = tk.StringVar(value=f"{curr_cash:.2f}")
        cash_entry = tk.Entry(cash_frame, textvariable=cash_var, width=12, bg="#eef2f7", bd=0, fg=TEXT, font=("Microsoft YaHei UI", 9, "bold"))
        cash_entry.pack(side=tk.RIGHT)

        # T+2 Pending Cash field
        t2_frame = tk.Frame(panel, bg=PANEL_BG)
        t2_frame.pack(fill=tk.X, pady=5)
        tk.Label(t2_frame, text="T+2待交收(CNY):", bg=PANEL_BG, fg=TEXT, font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)

        t2_var = tk.StringVar(value=f"{curr_t2:.2f}")
        t2_entry = tk.Entry(t2_frame, textvariable=t2_var, width=12, bg="#eef2f7", bd=0, fg=TEXT, font=("Microsoft YaHei UI", 9, "bold"))
        t2_entry.pack(side=tk.RIGHT)

        # Action button
        btn_frame = tk.Frame(panel, bg=PANEL_BG)
        btn_frame.pack(fill=tk.X, pady=(10, 0))

        status_lbl = tk.Label(btn_frame, text="", bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 8))
        status_lbl.pack(side=tk.LEFT)

        confirm_btn = tk.Label(
            btn_frame,
            text="确认",
            bg=BLUE,
            fg="#ffffff",
            font=("Microsoft YaHei UI", 8, "bold"),
            padx=12,
            pady=4,
            cursor="hand2",
        )
        confirm_btn.pack(side=tk.RIGHT)

        def do_correct(_event: Optional[tk.Event] = None) -> str:
            try:
                cash_val = float(cash_var.get().strip())
                t2_val = float(t2_var.get().strip())
                if cash_val < 0 or t2_val < 0:
                    raise ValueError("金额不能为负数")

                if self.demo:
                    status_lbl.configure(text="演示模式: 未写入")
                else:
                    _correct_real_cash(cash_val, t2_val)

                status_lbl.configure(text="成功")
                self.status_text.set("可用现金/待交收现金修正成功")
                self.refresh()
                self._close_cash_correction_popover()

                if not self.demo and self.remote_server_url:
                    if messagebox.askyesno("上传数据", "现金修正执行成功，是否上传最新的持仓配置数据到远程服务器？"):
                        self.upload_data()

                return "break"
            except Exception as exc:
                status_lbl.configure(text="失败")
                messagebox.showerror("错误", f"修正现金失败: {exc}")
                return "break"

        confirm_btn.bind("<Button-1>", do_correct)
        cash_entry.bind("<Return>", do_correct)
        t2_entry.bind("<Return>", do_correct)

        width = 240
        height = 140
        x = max(10, self.root.winfo_width() - width - 10)
        y = 50
        popover.place(x=x, y=y, width=width, height=height)
        popover.lift()
        self.root.tk.call("raise", popover._w)

    def _close_cash_correction_popover(self) -> None:
        if self.cash_popover and self.cash_popover.winfo_exists():
            self.cash_popover.destroy()
        self.cash_popover = None


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
