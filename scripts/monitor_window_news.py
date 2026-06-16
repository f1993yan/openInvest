"""Mixin methods for MonitorNewsMixin."""
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


class MonitorNewsMixin:
    def _wheel_news_stack(self, event: tk.Event) -> str:
        if self.news_scroll_canvas is not None:
            self._scroll_canvas(self.news_scroll_canvas, event)
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
        height = min(330, max(285, self.root.winfo_height() - 92))
        x = 14
        y = max(70, self.selection_bar.winfo_y() - height - 12)
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
        self.news_scroll_canvas = None
        self.news_pager_overlay = None
        self.news_cards = []
        self.news_source = ""

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
        self.news_scroll_canvas = None
        self.news_pager_overlay = None
        if not cards:
            tk.Label(body, text="暂无包含板块与龙头股的周末新闻总结", bg=BOARD_BG, fg=MUTED, font=("Microsoft YaHei UI", 10)).pack(expand=True)
            return
        self.news_page_index %= len(cards)
        self._create_neutral_stack_layers(body, len(cards))
        content = tk.Frame(body, bg=BOARD_BG)
        content.pack(fill=tk.BOTH, expand=True)
        card_host = tk.Frame(content, bg=BOARD_BG)
        card_host.pack(fill=tk.BOTH, expand=True)
        self._create_news_card(card_host, cards[self.news_page_index], managed=True)
        if len(cards) > 1:
            self._create_floating_news_pager(body, self.news_page_index, len(cards))

    def _create_floating_news_pager(self, parent: tk.Misc, index: int, total: int) -> None:
        overlay = tk.Frame(parent, bg=BOARD_BG)
        self.news_pager_overlay = overlay
        overlay.place(relx=1.0, rely=0.5, x=-2, anchor="e")
        up = self._create_news_pager_button(overlay, "↑", lambda: self._change_news_page(-1))
        up.pack(fill=tk.X)
        tk.Label(
            overlay,
            text=f"{index + 1}/{total}",
            bg=BOARD_BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(fill=tk.X, pady=4)
        down = self._create_news_pager_button(overlay, "↓", lambda: self._change_news_page(1))
        down.pack(fill=tk.X)
        overlay.lift()

    def _create_news_pager_button(self, parent: tk.Misc, text: str, command: Any) -> tk.Canvas:
        button = tk.Canvas(
            parent,
            width=30,
            height=30,
            bg=BOARD_BG,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )

        def draw(fill: str = "#ffffff") -> None:
            button.delete("all")
            button.create_oval(2, 2, 28, 28, fill=fill, outline="#cfe0f7", width=1)
            button.create_text(15, 14, text=text, fill=BLUE, font=("Segoe UI Symbol", 11, "bold"))

        draw()
        button.bind("<Enter>", lambda _event: draw("#e8f1ff"))
        button.bind("<Leave>", lambda _event: draw("#ffffff"))
        button.bind("<Button-1>", lambda _event: command())
        return button

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
        top.pack(fill=tk.X, padx=(0, 38))
        top.grid_columnconfigure(1, weight=1)
        tk.Label(top, text="●", bg=PANEL_BG, fg=heat_color, font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        title = tk.Label(
            top,
            text=_short(card_data.get("title", "周末机会"), 18),
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        )
        title.grid(row=0, column=1, sticky="ew", padx=(8, 6))
        tk.Label(top, text=f"热度 {heat:.0%}", bg=SOFT_BLUE, fg=BLUE, font=("Microsoft YaHei UI", 8, "bold"), padx=7, pady=2).grid(row=0, column=2, sticky="e")
        scroll_shell = tk.Frame(card, bg=PANEL_BG)
        scroll_shell.pack(fill=tk.BOTH, expand=True)
        tk.Frame(scroll_shell, bg=PANEL_BG, width=34).pack(side=tk.RIGHT, fill=tk.Y)
        self.news_scroll_canvas, content = _scrollable_frame(scroll_shell, bg=PANEL_BG)
        tk.Label(content, text=f"板块  {card_data.get('sector', '-')}", bg=PANEL_BG, fg=MUTED, font=("Microsoft YaHei UI", 9, "bold"), anchor="w").pack(fill=tk.X, pady=(12, 0))
        wrapping_labels: List[tk.Label] = []
        logic_label = tk.Label(content, text=str(card_data.get("logic") or "-"), bg=PANEL_BG, fg=TEXT, font=("Microsoft YaHei UI", 9), justify=tk.LEFT, anchor="w", wraplength=300)
        logic_label.pack(fill=tk.X, pady=(8, 0))
        wrapping_labels.append(logic_label)
        leaders = card_data.get("leaders") or []
        leader_text = "  ".join(
            f"{leader.get('name') or leader.get('symbol')}({leader.get('symbol', '-')})"
            for leader in leaders
        ) or "-"
        leader_label = tk.Label(content, text=f"龙头  {leader_text}", bg=PANEL_BG, fg=BLUE, font=("Microsoft YaHei UI", 9, "bold"), justify=tk.LEFT, anchor="w", wraplength=300)
        leader_label.pack(fill=tk.X, pady=(12, 0))
        wrapping_labels.append(leader_label)
        risk_label = tk.Label(content, text=f"风险  {card_data.get('risk_note') or '-'}", bg=PANEL_BG, fg=DOWN_FG, font=("Microsoft YaHei UI", 8), justify=tk.LEFT, anchor="w", wraplength=300)
        risk_label.pack(fill=tk.X, pady=(10, 0))
        wrapping_labels.append(risk_label)

        def update_wrap(event: tk.Event) -> None:
            wraplength = max(220, event.width - 14)
            for label in wrapping_labels:
                label.configure(wraplength=wraplength)

        content.bind("<Configure>", update_wrap, add="+")
        for widget in card.winfo_children():
            widget.bind("<Enter>", lambda _event: self._set_hover_stack("news"))
            widget.bind("<MouseWheel>", lambda event: self._wheel_news_stack(event))
        return card


