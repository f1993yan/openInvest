"""Mixin methods for MonitorTradeMixin."""
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


class MonitorTradeMixin:
    def _toggle_trade_popover(self) -> None:
        if self.trade_popover and self.trade_popover.winfo_exists():
            self._close_trade_popover()
            return
        self._open_trade_popover()

    def _open_trade_popover(self) -> None:
        self._close_trade_popover()
        popover = tk.Frame(self.root, bg=LINE, padx=1, pady=1)
        self.trade_popover = popover
        panel = tk.Frame(popover, bg=PANEL_BG, padx=10, pady=8)
        panel.pack(fill=tk.BOTH, expand=True)
        top = tk.Frame(panel, bg=PANEL_BG)
        top.pack(fill=tk.X)
        tk.Label(
            top,
            text="手动记账",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side=tk.LEFT)
        close_btn = tk.Label(top, text="×", bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 12), width=2, cursor="hand2")
        close_btn.pack(side=tk.RIGHT)
        close_btn.bind("<Button-1>", lambda _event: self._close_trade_popover())

        header = tk.Frame(panel, bg=PANEL_BG)
        header.pack(fill=tk.X, pady=(8, 2), padx=(0, 9))
        widths = (128, 54, 82, 44, 52)
        for idx, (text, width) in enumerate((("标的", 128), ("买/卖", 54), ("手数", 82), ("持仓", 44), ("", 52))):
            header.grid_columnconfigure(idx, minsize=width)
            tk.Label(
                header,
                text=text,
                bg=PANEL_BG,
                fg=MUTED,
                font=("Microsoft YaHei UI", 8, "bold"),
                anchor="w",
            ).grid(row=0, column=idx, sticky="ew")

        self.trade_scroll_canvas, rows_frame = _scrollable_frame(panel, bg=PANEL_BG)
        rows = _trade_panel_rows() if not self.demo else [
            {"symbol": "002185", "name": "华天科技", "source": "持仓", "units": 300},
            {"symbol": "000001", "name": "示例标的2", "source": "关注", "units": 0},
        ]
        if not rows:
            tk.Label(rows_frame, text="暂无持仓或关注标的", bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(fill=tk.X, pady=12)
        for row in rows[:20]:
            self._create_trade_row(rows_frame, row)

        self.root.update_idletasks()
        width = min(402, max(360, self.root.winfo_width() - 24))
        height = min(400, max(148, 62 + max(2, min(len(rows), 15)) * 30))
        x = max(10, self.root.winfo_width() - width - 10)
        y = max(58, self.root.winfo_height() - height - 70)
        popover.place(x=x, y=y, width=width, height=height)
        popover.lift()
        self.root.tk.call("raise", popover._w)

    def _create_trade_row(self, parent: tk.Misc, row: Dict[str, Any]) -> None:
        line = tk.Frame(parent, bg=PANEL_BG)
        line.pack(fill=tk.X, pady=2)
        for idx, width in enumerate((128, 54, 82, 44, 52)):
            line.grid_columnconfigure(idx, minsize=width)
        name = str(row.get("name") or row.get("symbol") or "-")
        symbol = str(row.get("symbol") or "").strip()
        lot_size = max(1, int(_safe_num(row.get("min_lot_size"), 100) or 100))
        holding_lots = int(_safe_num(row.get("units")) // lot_size)
        name_text = f"{name} {symbol}" if symbol else name
        tk.Label(
            line,
            text=_short(name_text, 15),
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        direction_var = tk.StringVar(value="买")
        direction_btn = tk.Label(
            line,
            textvariable=direction_var,
            bg="#eef3f8",
            fg=TEXT,
            font=("Microsoft YaHei UI", 8, "bold"),
            width=5,
            cursor="hand2",
        )
        direction_btn.grid(row=0, column=1, sticky="ew", padx=(0, 4), ipady=3)

        def toggle_direction(_event: Optional[tk.Event] = None) -> str:
            direction_var.set("卖" if direction_var.get() == "买" else "买")
            direction_btn.configure(bg="#fff3f0" if direction_var.get() == "卖" else "#eef3f8")
            return "break"

        direction_btn.bind("<Button-1>", toggle_direction)
        lots_var = tk.StringVar(value="1")
        lot_box = tk.Frame(line, bg=PANEL_BG)
        lot_box.grid(row=0, column=2, sticky="w", padx=(0, 4))
        dec_btn = tk.Label(lot_box, text="<", bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 9), cursor="hand2", width=1)
        dec_btn.pack(side=tk.LEFT)
        lots_entry = tk.Entry(
            lot_box,
            textvariable=lots_var,
            width=3,
            bd=0,
            bg="#eef2f7",
            fg=TEXT,
            justify=tk.CENTER,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        lots_entry.pack(side=tk.LEFT, ipady=2)
        inc_btn = tk.Label(lot_box, text=">", bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 9), cursor="hand2", width=1)
        inc_btn.pack(side=tk.LEFT, padx=(0, 5))

        def adjust_lots(step: int) -> str:
            try:
                current = int(lots_var.get().strip() or "0")
            except ValueError:
                current = 0
            lots_var.set(str(max(1, current + step)))
            return "break"

        dec_btn.bind("<Button-1>", lambda _event: adjust_lots(-1))
        inc_btn.bind("<Button-1>", lambda _event: adjust_lots(1))
        status_var = tk.StringVar(value=f"{holding_lots}手" if holding_lots > 0 else "")
        tk.Label(line, textvariable=status_var, bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 8), anchor="w").grid(row=0, column=3, sticky="ew")
        exec_btn = tk.Label(
            line,
            text="执行",
            bg=BLUE,
            fg="#ffffff",
            font=("Microsoft YaHei UI", 8, "bold"),
            padx=8,
            pady=2,
            cursor="hand2",
        )
        exec_btn.grid(row=0, column=4, sticky="ew", padx=(4, 0), ipady=2)

        def execute(_event: Optional[tk.Event] = None) -> str:
            try:
                if row.get("error"):
                    raise ValueError(str(row.get("name") or "读取失败"))
                raw = lots_var.get().strip()
                if not raw.isdigit():
                    raise ValueError("手数必须是整数")
                lots = int(raw)
                direction = "SELL" if direction_var.get() == "卖" else "BUY"
                if direction == "SELL" and lots > holding_lots:
                    raise ValueError(f"最多卖出 {holding_lots} 手")
                if self.demo:
                    message = f"演示模式: {name} {direction} {lots}手"
                else:
                    message = _execute_user_trade_manual(symbol, direction, lots, lot_size)
                status_var.set("完成")
                self.status_text.set(message)
                self.refresh()
                self._close_trade_popover()
                return "break"
            except Exception as exc:  # noqa: BLE001
                status_var.set("失败")
                self.status_text.set(f"记账失败: {exc}")
                return "break"

        exec_btn.bind("<Button-1>", execute)
        lots_entry.bind("<Return>", execute)

    def _close_trade_popover(self) -> None:
        if self.trade_popover and self.trade_popover.winfo_exists():
            self.trade_popover.destroy()
        self.trade_popover = None
        self.trade_scroll_canvas = None


