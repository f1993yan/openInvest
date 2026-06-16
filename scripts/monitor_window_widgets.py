"""Reusable Tk widgets for the desktop monitor window."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Optional

from scripts.monitor_window_constants import BLUE, BOARD_BG, LINE, PANEL_BG, TEXT
from scripts.monitor_window_services import _cash_ratio, _fmt_cash_line

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
        font: tuple[str, int, str] = ("Segoe UI", 18, "bold"),
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
        self.font = font
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
            font=self.font,
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


def _scrollable_frame(parent: tk.Misc, *, bg: str = PANEL_BG) -> tuple[tk.Canvas, tk.Frame]:
    shell = tk.Frame(parent, bg=bg)
    shell.pack(fill=tk.BOTH, expand=True)
    canvas = tk.Canvas(shell, bg=bg, highlightthickness=0, bd=0)
    scrollbar = ttk.Scrollbar(
        shell,
        orient=tk.VERTICAL,
        command=canvas.yview,
        style="App.Vertical.TScrollbar",
    )
    content = tk.Frame(canvas, bg=bg)
    window = canvas.create_window((0, 0), window=content, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(4, 0))

    def update_scrollregion(_event: Optional[tk.Event] = None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def update_width(event: tk.Event) -> None:
        canvas.itemconfigure(window, width=event.width)

    def wheel(event: tk.Event) -> str:
        canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def bind_child_wheel(widget: tk.Misc) -> None:
        try:
            widget.bind("<MouseWheel>", wheel, add="+")
            for child in widget.winfo_children():
                bind_child_wheel(child)
        except Exception:
            return

    def refresh_child_bindings() -> None:
        if not canvas.winfo_exists():
            return
        bind_child_wheel(content)
        canvas.after(500, refresh_child_bindings)

    content.bind("<Configure>", update_scrollregion)
    canvas.bind("<Configure>", update_width)
    canvas.bind("<MouseWheel>", wheel)
    content.bind("<MouseWheel>", wheel)
    canvas.after(50, refresh_child_bindings)
    return canvas, content




__all__ = [
    "RoundedButton",
    "CircleButton",
    "CashRatioBar",
    "_scrollable_frame",
]
