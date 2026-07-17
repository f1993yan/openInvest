"""Reusable Tk widgets for the desktop monitor window."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Optional

from scripts.monitor_window_constants import BLUE, BOARD_BG, LINE, PANEL_BG, TEXT
from scripts.monitor_window_services import _cash_ratio, _fmt_cash_line, _safe_num

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
        self.pending_ratio = 0.0
        self.bind("<Configure>", lambda _event: self._draw())
        self._draw()

    def set_values(self, cash: Any, total_assets: Any, pending_cash: Any = 0) -> None:
        self.cash_text = _fmt_cash_line(cash, total_assets, pending_cash)
        self.ratio = 1.0 - _cash_ratio(_safe_num(cash) + _safe_num(pending_cash), total_assets)
        self.pending_ratio = max(0.0, min(_cash_ratio(pending_cash, total_assets), 1.0))
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
        pending_w = max(0, int(inner_w * self.pending_ratio))
        self._rounded_rect(pad, pad, width - pad, height - pad, 7, fill="#e8eef7", outline=LINE)
        if fill_w > 0:
            if fill_w >= inner_w - 2:
                self._rounded_rect(pad + 1, pad + 1, width - pad - 1, height - pad - 1, 6, fill="#9ec5ff")
            else:
                self.create_rectangle(pad + 1, pad + 1, pad + fill_w, height - pad - 1, fill="#9ec5ff", outline="")
        if pending_w > 0:
            x1 = min(width - pad - 1, pad + fill_w)
            x2 = min(width - pad - 1, x1 + pending_w)
            if x2 > x1:
                self.create_rectangle(x1, pad + 1, x2, height - pad - 1, fill="#f2b35d", outline="")
        self.create_text(
            width - 9,
            height / 2,
            text=self.cash_text,
            anchor="e",
            fill=TEXT,
            font=("Microsoft YaHei UI", 8, "bold"),
        )


class ModeSlider(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        options: tuple[tuple[str, str], ...],
        command: Any,
        width: int = 92,
        height: int = 22,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=BOARD_BG,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.options = options
        self.command = command
        self.active = options[0][0] if options else ""
        self._dragging = False
        self._icon_cache: dict[tuple[str, bool, str], tk.PhotoImage] = {}
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def set_active(self, value: str) -> None:
        if value == self.active:
            self._draw()
            return
        if any(key == value for key, _label in self.options):
            self.active = value
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

    def _index_for_x(self, x: int) -> int:
        if not self.options:
            return 0
        width = max(self.winfo_width(), 1)
        track_x1 = 13
        track_x2 = width - 13
        if len(self.options) <= 1:
            return 0
        step = max((track_x2 - track_x1) / (len(self.options) - 1), 1)
        return max(0, min(len(self.options) - 1, int(round((x - track_x1) / step))))

    def _set_by_index(self, index: int, *, notify: bool) -> None:
        if not self.options:
            return
        key = self.options[index][0]
        changed = key != self.active
        self.active = key
        self._draw()
        if changed and notify:
            self.command(key)

    def _on_press(self, event: tk.Event) -> str:
        self._dragging = True
        self._set_by_index(self._index_for_x(event.x), notify=False)
        return "break"

    def _on_drag(self, event: tk.Event) -> str:
        self._set_by_index(self._index_for_x(event.x), notify=False)
        return "break"

    def _on_release(self, event: tk.Event) -> str:
        self._dragging = False
        self._set_by_index(self._index_for_x(event.x), notify=True)
        return "break"

    def _blend(self, fg: tuple[int, int, int], bg: tuple[int, int, int], alpha: float) -> str:
        alpha = max(0.0, min(alpha, 1.0))
        mixed = tuple(round(bg[i] * (1 - alpha) + fg[i] * alpha) for i in range(3))
        return "#%02x%02x%02x" % mixed

    def _hex_rgb(self, color: str) -> tuple[int, int, int]:
        return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))

    def _aa_icon(self, key: str, *, active: bool, color: str) -> tk.PhotoImage:
        cache_key = (key, active, color)
        cached = self._icon_cache.get(cache_key)
        if cached is not None:
            return cached
        size = 22 if active else 16
        scale = 4
        hi = size * scale
        bg = self._hex_rgb(BOARD_BG)
        fg = self._hex_rgb(color)
        white = (255, 255, 255)
        center = hi / 2
        radius = (hi - 2 * scale) / 2
        inner_radius = radius - (2.2 * scale if active else 1.4 * scale)
        samples = ((0.20, 0.20), (0.80, 0.20), (0.20, 0.80), (0.80, 0.80))

        def dist_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
            vx, vy = bx - ax, by - ay
            wx, wy = px - ax, py - ay
            denom = vx * vx + vy * vy
            t = 0.0 if denom <= 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
            cx, cy = ax + t * vx, ay + t * vy
            return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5

        def in_profit(px: float, py: float) -> bool:
            line = dist_to_segment(px, py, hi * 0.30, hi * 0.62, hi * 0.66, hi * 0.36) <= hi * 0.045
            head_a = dist_to_segment(px, py, hi * 0.66, hi * 0.36, hi * 0.66, hi * 0.55) <= hi * 0.040
            head_b = dist_to_segment(px, py, hi * 0.66, hi * 0.36, hi * 0.48, hi * 0.36) <= hi * 0.040
            return line or head_a or head_b

        def in_cash(px: float, py: float) -> bool:
            outer = ((px - center) ** 2 + (py - center) ** 2) ** 0.5
            bar = abs(px - center) <= hi * 0.045 and hi * 0.28 <= py <= hi * 0.72
            arc_top = dist_to_segment(px, py, hi * 0.38, hi * 0.38, hi * 0.62, hi * 0.38) <= hi * 0.040
            arc_mid = dist_to_segment(px, py, hi * 0.36, hi * 0.50, hi * 0.64, hi * 0.50) <= hi * 0.038
            arc_bot = dist_to_segment(px, py, hi * 0.38, hi * 0.62, hi * 0.62, hi * 0.62) <= hi * 0.040
            return (inner_radius * 0.58 <= outer <= inner_radius * 0.86) or bar or arc_top or arc_mid or arc_bot

        icon_test = {"active_profit": in_profit, "cash_recovery": in_cash}.get(key, in_profit)
        image = tk.PhotoImage(width=size, height=size)
        for y in range(size):
            row = []
            for x in range(size):
                fill_hits = 0
                outline_hits = 0
                glyph_hits = 0
                for sx, sy in samples:
                    px = (x + sx) * scale
                    py = (y + sy) * scale
                    d = ((px - center) ** 2 + (py - center) ** 2) ** 0.5
                    if d <= inner_radius:
                        fill_hits += 1
                    if inner_radius < d <= radius:
                        outline_hits += 1
                    if icon_test(px, py):
                        glyph_hits += 1
                if glyph_hits:
                    base = fg if not active else white
                    row.append(self._blend(base, fg if active else bg, glyph_hits / len(samples)))
                elif fill_hits:
                    base = fg if active else bg
                    row.append(self._blend(base, bg, fill_hits / len(samples)))
                elif outline_hits:
                    base = white if active else fg
                    row.append(self._blend(base, bg, outline_hits / len(samples)))
                else:
                    row.append(BOARD_BG)
            image.put("{" + " ".join(row) + "}", to=(0, y))
        self._icon_cache[cache_key] = image
        return image

    def _draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 2)
        height = max(self.winfo_height(), 2)
        count = max(len(self.options), 1)
        track_x1 = 13
        track_x2 = width - 13
        track_y = height / 2
        self.create_line(
            track_x1,
            track_y,
            track_x2,
            track_y,
            fill="#d8e2f0",
            width=3,
            capstyle=tk.ROUND,
        )
        active_idx = next((idx for idx, (key, _label) in enumerate(self.options) if key == self.active), 0)
        dot_step = (track_x2 - track_x1) / max(count - 1, 1)
        colors = {
            "active_profit": "#12b76a",
            "cash_recovery": "#f79009",
        }
        active_color = colors.get(self.active, BLUE)
        active_x = track_x1 + active_idx * dot_step
        self.create_line(track_x1, track_y, active_x, track_y, fill=active_color, width=3, capstyle=tk.ROUND)
        for idx, (key, _label) in enumerate(self.options):
            x = track_x1 + idx * dot_step
            is_active = key == self.active
            color = colors.get(key, BLUE)
            image = self._aa_icon(key, active=is_active, color=color)
            self.create_image(x, track_y, image=image)


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
    "ModeSlider",
    "_scrollable_frame",
]
