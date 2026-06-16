"""Mixin methods for MonitorAnalysisMixin."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from typing import Any, Dict, List, Optional

from scripts.monitor_window_constants import *
from scripts.monitor_window_services import *
from scripts.monitor_window_text import *
from scripts.monitor_window_widgets import *


class MonitorAnalysisMixin:
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
        tk.Label(summary_mid, text=f"{_verdict_signal(op.get('verdict'))} {_verdict_label(op.get('verdict'))}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 10)).pack(side=tk.RIGHT)

        summary_bottom = tk.Frame(summary, bg=_card_bg(row))
        summary_bottom.pack(fill=tk.X, pady=(8, 0))
        tk.Label(summary_bottom, text=f"买 {_buy_summary(row)}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        tk.Label(summary_bottom, text=f"卖 {_exit_summary(row)}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=(12, 0))
        tk.Label(summary_bottom, text=f"基 {float(_safe_num(fundamental.get('score'), 50)):.0f}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT)

        progress_wrap = tk.Frame(body, bg=PANEL_BG, padx=12, pady=10)
        progress_wrap.pack(fill=tk.X, pady=(12, 0))
        tk.Label(
            progress_wrap,
            text="正在获取最新价格并运行委员会分析",
            bg=PANEL_BG,
            fg=BLUE,
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
        ).pack(fill=tk.X)
        progress = ttk.Progressbar(
            progress_wrap,
            mode="indeterminate",
            style="Analysis.Horizontal.TProgressbar",
        )
        progress.pack(fill=tk.X, pady=(8, 0))
        progress.start(12)

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
        text.configure(state=tk.DISABLED)
        dialog._analysis_text = text  # type: ignore[attr-defined]
        dialog._analysis_status = status_label  # type: ignore[attr-defined]
        dialog._analysis_progress = progress  # type: ignore[attr-defined]
        dialog._analysis_progress_wrap = progress_wrap  # type: ignore[attr-defined]

        threading.Thread(
            target=self._analysis_worker,
            args=(symbol, row),
            daemon=True,
        ).start()

    def _close_dialog(self, symbol: str, dialog: tk.Toplevel) -> None:
        self.dialogs.pop(symbol, None)
        progress = getattr(dialog, "_analysis_progress", None)
        if progress is not None:
            try:
                progress.stop()
            except Exception:
                pass
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
                progress = getattr(dialog, "_analysis_progress", None)
                progress_wrap = getattr(dialog, "_analysis_progress_wrap", None)
                if progress is not None:
                    try:
                        progress.stop()
                    except Exception:
                        pass
                if progress_wrap is not None and progress_wrap.winfo_exists():
                    progress_wrap.destroy()
                if text is not None:
                    row = next((item for item in self.current_rows if item.get("symbol") == symbol), {})
                    text.configure(state=tk.NORMAL)
                    text.delete("1.0", tk.END)
                    text.insert(tk.END, _format_committee_result(row, result))
                    text.configure(state=tk.DISABLED)
                    text.see("1.0")
                    if status_label is not None:
                        if result.get("success"):
                            status_label.configure(text="委员会分析已完成", fg=UP_FG)
                        else:
                            status_label.configure(text="委员会分析失败", fg=DOWN_FG)
                    dialog.lift()
        self.root.after(250, self._poll_analysis_queue)


