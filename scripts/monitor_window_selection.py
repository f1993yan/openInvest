"""Mixin methods for MonitorSelectionMixin."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from typing import Any, Dict, List, Optional

from core.buy_signal_miner import buy_signal_summary_text
from scripts.monitor_window_constants import *
from scripts.monitor_window_services import *
from scripts.monitor_window_text import *
from scripts.monitor_window_widgets import *


class MonitorSelectionMixin:
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
            label.bind("<MouseWheel>", self._wheel_selection_bar)
        self._on_selection_buttons_configure()

    def _on_selection_buttons_configure(self, _event: Optional[tk.Event] = None) -> None:
        if self.selection_buttons_canvas is None:
            return
        self.selection_buttons_canvas.configure(scrollregion=self.selection_buttons_canvas.bbox("all"))
        self._bind_selection_bar_wheel(self.selection_buttons_frame)

    def _wheel_selection_bar(self, event: tk.Event) -> str:
        if self.selection_buttons_canvas is not None:
            self.selection_buttons_canvas.xview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _bind_selection_bar_wheel(self, widget: tk.Misc) -> None:
        try:
            widget.bind("<MouseWheel>", self._wheel_selection_bar, add="+")
            for child in widget.winfo_children():
                self._bind_selection_bar_wheel(child)
        except tk.TclError:
            return

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

        action_bar = tk.Frame(panel, bg=PANEL_BG)
        action_bar.pack(fill=tk.X, side=tk.BOTTOM, pady=(8, 0))
        self.selection_scroll_canvas, content = _scrollable_frame(panel, bg=PANEL_BG)
        self._render_selection_reason(content, action_bar, stock)
        pointer = tk.Canvas(popover, width=24, height=12, bg=BOARD_BG, highlightthickness=0, bd=0)
        pointer.create_polygon(2, 0, 22, 0, 12, 12, fill=LINE, outline=LINE)
        pointer.place(relx=0.5, rely=1.0, y=-1, anchor="n")
        self.root.update_idletasks()
        width = min(370, max(300, self.root.winfo_width() - 28))
        height = min(330, max(292, self.root.winfo_height() - 112))
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
        self.selection_scroll_canvas = None

    def _current_watchlist_symbols(self) -> set:
        if self.demo:
            return set()
        try:
            from jobs.market_monitor import load_config
            config = load_config()
            return {
                str(item.get("symbol") or "").strip()
                for bucket in (config.get("holdings") or [], config.get("watchlist") or [])
                for item in bucket
                if item.get("symbol")
            }
        except Exception:
            return set()

    def _render_selection_reason(self, body: tk.Frame, action_bar: tk.Frame, stock: Dict[str, Any]) -> None:
        tape = stock.get("tape") or {}
        trend = stock.get("trend") or {}
        plan = stock.get("entry_plan") or {}
        path = stock.get("path_distribution") or {}
        risk = stock.get("risk_defense") or {}
        calibration = stock.get("calibration") or {}
        buy_signal = stock.get("buy_signal_backtest") or {}
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
        tk.Label(
            card,
            text=buy_signal_summary_text(buy_signal),
            bg=PANEL_BG,
            fg=BLUE,
            font=("Microsoft YaHei UI", 8, "bold"),
            anchor="w",
            justify=tk.LEFT,
            wraplength=330,
        ).pack(fill=tk.X, pady=(6, 0))
        trigger = _fmt_price(plan.get("trigger_price"))
        stop = _fmt_price(plan.get("stop_loss_price"))
        tk.Label(card, text=f"买点  {plan.get('action', '-')}  触发 {trigger}  止损 {stop}", bg=PANEL_BG, fg=UP_FG, font=("Microsoft YaHei UI", 8, "bold"), anchor="w").pack(fill=tk.X, pady=(7, 0))
        tk.Label(card, text=f"备注  {_short(plan.get('note'), 92) or '-'}", bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 8), justify=tk.LEFT, anchor="w", wraplength=330).pack(fill=tk.X, pady=(6, 0))
        status_var = tk.StringVar(value="")
        symbol = str(stock.get("symbol") or "").strip()
        already_added = symbol in self._current_watchlist_symbols()
        btn_text = "已关注" if already_added else "加入关注列表"
        btn_fg = UP_FG if already_added else TEXT
        add_btn = tk.Label(
            action_bar,
            text=btn_text,
            bg="#eef3f8",
            fg=btn_fg,
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
                add_btn.configure(text="已关注", fg=UP_FG)
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
        from jobs.market_monitor import load_config
        from db.account_ledger import AccountLedger, REAL_ACCOUNT

        config = load_config()
        all_symbols = {
            str(item.get("symbol") or "").strip()
            for item in list(config.get("holdings") or []) + list(config.get("watchlist") or [])
        }
        if symbol in all_symbols:
            return "已在持仓或关注列表"
        ledger = AccountLedger()
        ledger.ensure_initialized(config)
        added = ledger.add_holding(
            account=REAL_ACCOUNT,
            symbol=symbol,
            name=stock.get("name") or symbol,
            market="a",
            sector=stock.get("sector") or "",
            industry=stock.get("industry") or "",
            units=0,
            cost=0,
            min_lot_size=100,
        )
        if not added:
            return "已在持仓或关注列表"
        return f"已加入关注列表: {stock.get('name') or symbol}"


