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


def _configure_analysis_text_tags(text: tk.Text) -> None:
    text.tag_configure("risk", foreground=DOWN_FG, font=("Microsoft YaHei UI", 10, "bold"))
    text.tag_configure("positive", foreground=UP_FG, font=("Microsoft YaHei UI", 10, "bold"))
    text.tag_configure("muted", foreground=MUTED, font=("Microsoft YaHei UI", 10))
    text.tag_configure("heading", foreground=TEXT, font=("Microsoft YaHei UI", 10, "bold"))
    text.tag_configure("normal", foreground=TEXT, font=("Microsoft YaHei UI", 10))


def _insert_styled_analysis_text(text: tk.Text, content: str) -> None:
    _configure_analysis_text_tags(text)
    lines = str(content or "").splitlines()
    if not lines:
        return
    for index, line in enumerate(lines):
        tag = _detail_line_style(line)
        text.insert(tk.END, line, tag)
        if index < len(lines) - 1:
            text.insert(tk.END, "\n", "normal")


def _latest_committee_status_for_dialog(row: Dict[str, Any], result: Dict[str, Any], verdict: str, alloc: float) -> str:
    previous_state = str((row or {}).get("state") or "")
    if previous_state == "action_required":
        return "action_required"
    if abs(alloc) <= 0:
        return previous_state or "monitoring"
    is_holding = bool((row or {}).get("is_holding")) or _safe_num((row or {}).get("units")) > 0 or _safe_num((row or {}).get("position_pct")) > 0
    if verdict == "SELL" and alloc < 0 and is_holding:
        return "action_required"
    return "candidate"


class MonitorAnalysisMixin:
    def _open_analysis_dialog(self, symbol: str, row: Optional[Dict[str, Any]] = None) -> None:
        if not row:
            row = next((item for item in self.current_rows if item.get("symbol") == symbol), None)
        if not row:
            return
        existing = self.dialogs.get(symbol)
        if existing and existing.winfo_exists():
            existing.deiconify()
            existing.focus_force()
            existing.lift()
            return

        dialog = tk.Toplevel(self.root)
        dialog._row = row  # Store row on the dialog
        dialog.title(f"{row.get('name', symbol)} {symbol} 最新委员会分析" if not row.get("_is_resolving") else f"正在检索标的 '{row.get('name')}'...")
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
            text=f"{row.get('name', symbol)}  {symbol}" if not row.get("_is_resolving") else f"检索中: {row.get('name')}",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=14,
        )
        title.pack(side=tk.LEFT, fill=tk.Y)
        status_label = tk.Label(
            titlebar,
            text="正在获取最新分析..." if not row.get("_is_resolving") else "正在联网检索标的信息...",
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

        summary_container = tk.Frame(body, bg=BOARD_BG)
        summary_container.pack(fill=tk.X)

        if row.get("_is_resolving"):
            placeholder = tk.Label(
                summary_container,
                text=f"正在联网检索 '{row.get('name')}' 的实时行情数据...",
                bg=PANEL_BG,
                fg=MUTED,
                font=("Microsoft YaHei UI", 10),
                pady=24,
            )
            placeholder.pack(fill=tk.X)
            dialog._summary_placeholder = placeholder  # type: ignore[attr-defined]
        else:
            self._build_dialog_summary(summary_container, symbol, row)

        progress_wrap = tk.Frame(body, bg=PANEL_BG, padx=12, pady=10)
        progress_wrap.pack(fill=tk.X, pady=(12, 0))
        progress_text = "正在联网解析标的信息..." if row.get("_is_resolving") else "正在获取最新价格并运行委员会分析"
        progress_label = tk.Label(
            progress_wrap,
            text=progress_text,
            bg=PANEL_BG,
            fg=BLUE,
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
        )
        progress_label.pack(fill=tk.X)
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
        initial_text = f"正在联网解析关键词 '{row.get('name')}' 的行情数据..." if row.get("_is_resolving") else _snapshot_detail(row)
        _insert_styled_analysis_text(text, initial_text)
        text.configure(state=tk.DISABLED)
        dialog._analysis_text = text  # type: ignore[attr-defined]
        dialog._analysis_status = status_label  # type: ignore[attr-defined]
        dialog._analysis_progress = progress  # type: ignore[attr-defined]
        dialog._analysis_progress_wrap = progress_wrap  # type: ignore[attr-defined]
        dialog._analysis_summary_container = summary_container  # type: ignore[attr-defined]

        if row.get("_is_resolving"):
            threading.Thread(
                target=self._resolve_and_analyze_worker,
                args=(symbol, dialog, progress_wrap, progress, text, title, status_label, body, summary_container, progress_label),
                daemon=True,
            ).start()
        else:
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
                    row = getattr(dialog, "_row", {})
                    text.configure(state=tk.NORMAL)
                    text.delete("1.0", tk.END)
                    _insert_styled_analysis_text(text, _format_committee_result(row, result))
                    text.configure(state=tk.DISABLED)
                    text.see("1.0")
                    if result.get("success"):
                        refreshed_row = _dialog_row_from_committee_result(row, result)
                        dialog._row = refreshed_row
                        if hasattr(self, "_apply_committee_row_update"):
                            self._apply_committee_row_update(refreshed_row)
                        summary_container = getattr(dialog, "_analysis_summary_container", None)
                        if summary_container is not None and summary_container.winfo_exists():
                            for child in summary_container.winfo_children():
                                child.destroy()
                            self._build_dialog_summary(summary_container, symbol, refreshed_row)
                    if status_label is not None:
                        if result.get("success"):
                            status_label.configure(text="委员会分析已完成", fg=UP_FG)
                        else:
                            status_label.configure(text="委员会分析失败", fg=DOWN_FG)
                    dialog.lift()
        self.root.after(250, self._poll_analysis_queue)

    def _build_dialog_summary(self, parent: tk.Frame, symbol: str, row: Dict[str, Any]) -> None:
        op = row.get("operation") or {}
        price = row.get("price") or {}
        fundamental = row.get("fundamental") or {}
        summary = tk.Frame(parent, bg=_card_bg(row), padx=12, pady=10)
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

        if hasattr(self, "_current_watchlist_symbols") and hasattr(self, "_add_selection_to_watchlist"):
            clean_symbol = symbol.strip().upper()
            if clean_symbol not in self._current_watchlist_symbols():
                add_btn = tk.Label(
                    summary_top,
                    text="加入关注",
                    bg="#eef3f8",
                    fg=TEXT,
                    font=("Microsoft YaHei UI", 8, "bold"),
                    padx=8,
                    pady=2,
                    relief=tk.SOLID,
                    bd=1,
                    cursor="hand2",
                )
                add_btn.pack(side=tk.RIGHT, padx=(0, 10))

                def on_add_btn_click(_event: Optional[tk.Event] = None, s_btn=add_btn) -> str:
                    try:
                        stock = {
                            "symbol": clean_symbol,
                            "name": row.get("name") or clean_symbol,
                            "sector": row.get("sector") or "",
                        }
                        message = self._add_selection_to_watchlist(stock)
                        s_btn.configure(text="已关注", fg=UP_FG, bg="#e2e8f0")
                        self.status_text.set(message)
                        return "break"
                    except Exception as exc:
                        self.status_text.set(f"失败: {exc}")
                        return "break"

                add_btn.bind("<Button-1>", on_add_btn_click)
                add_btn._click_handler = on_add_btn_click
                add_btn.bind("<Enter>", lambda _event: add_btn.configure(bg="#e2e8f0"))
                add_btn.bind("<Leave>", lambda _event: add_btn.configure(bg="#eef3f8" if add_btn.cget("text") == "加入关注" else "#e2e8f0"))

        summary_mid = tk.Frame(summary, bg=_card_bg(row))
        summary_mid.pack(fill=tk.X, pady=(8, 0))
        tk.Label(summary_mid, text=_fmt_price(price.get("current")), bg=_card_bg(row), fg=TEXT, font=("Microsoft YaHei UI", 18, "bold")).pack(side=tk.LEFT)
        tk.Label(summary_mid, text=_fmt_pct(price.get("change_pct")), bg=_card_bg(row), fg=_change_color(row), font=("Microsoft YaHei UI", 11, "bold")).pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(summary_mid, text=f"{_verdict_signal(op.get('verdict'))} {_verdict_label(op.get('verdict'))}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 10)).pack(side=tk.RIGHT)

        summary_bottom = tk.Frame(summary, bg=_card_bg(row))
        summary_bottom.pack(fill=tk.X, pady=(8, 0))
        tk.Label(summary_bottom, text=_sector_summary(row), bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        tk.Label(summary_bottom, text=f"基 {float(_safe_num(fundamental.get('score'), 50)):.0f}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.RIGHT)
        summary_levels = tk.Frame(summary, bg=_card_bg(row))
        summary_levels.pack(fill=tk.X, pady=(4, 0))
        tk.Label(summary_levels, text=f"买 {_buy_summary(row)}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)
        tk.Label(summary_levels, text=f"卖 {_exit_summary(row)}", bg=_card_bg(row), fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=(12, 0))

    def _resolve_and_analyze_worker(
        self,
        query: str,
        dialog: tk.Toplevel,
        progress_wrap: tk.Frame,
        progress: ttk.Progressbar,
        text_box: tk.Text,
        title_label: tk.Label,
        status_label: tk.Label,
        body: tk.Frame,
        summary_container: tk.Frame,
        progress_label: tk.Label,
    ) -> None:
        try:
            resolved = self._resolve_query_to_stock(query)
            if not resolved:
                def on_fail():
                    if dialog.winfo_exists():
                        progress.stop()
                        progress_wrap.destroy()
                        status_label.configure(text="未找到标的", fg=DOWN_FG)
                        text_box.configure(state=tk.NORMAL)
                        text_box.delete("1.0", tk.END)
                        _insert_styled_analysis_text(text_box, f"联网检索失败: 未找到与关键词 '{query}' 相关的标的。")
                        text_box.configure(state=tk.DISABLED)
                self.root.after(0, on_fail)
                return

            symbol, name, price_info = resolved
            dummy_row = {
                "symbol": symbol,
                "name": name,
                "market": "hk" if len(symbol) == 5 else "a",
                "state": "watch",
                "operation": {
                    "verdict": "HOLD",
                },
                "price": {
                    "current": price_info.get("price", 0.0),
                    "change_pct": price_info.get("change_pct", 0.0),
                },
                "fundamental": {
                    "score": 50,
                },
                "position_pct": 0,
                "units": 0,
            }

            def on_success():
                if not dialog.winfo_exists():
                    return
                self.dialogs.pop(query, None)
                existing = self.dialogs.get(symbol)
                if existing and existing.winfo_exists():
                    existing.deiconify()
                    existing.focus_force()
                    existing.lift()
                    dialog.destroy()
                    return
                self.dialogs[symbol] = dialog
                dialog._row = dummy_row

                dialog.title(f"{name} {symbol} 最新委员会分析")
                title_label.configure(text=f"{name}  {symbol}")
                status_label.configure(text="正在分析中...", fg=BLUE)

                if hasattr(dialog, "_summary_placeholder") and dialog._summary_placeholder.winfo_exists():
                    dialog._summary_placeholder.destroy()

                self._build_dialog_summary(summary_container, symbol, dummy_row)

                progress_label.configure(text="正在获取最新价格并运行委员会分析")
                text_box.configure(state=tk.NORMAL)
                text_box.delete("1.0", tk.END)
                _insert_styled_analysis_text(text_box, _snapshot_detail(dummy_row))
                text_box.configure(state=tk.DISABLED)

                threading.Thread(
                    target=self._analysis_worker,
                    args=(symbol, dummy_row),
                    daemon=True,
                ).start()

            self.root.after(0, on_success)
        except Exception as e:
            def on_error(err_msg=str(e)):
                if dialog.winfo_exists():
                    progress.stop()
                    progress_wrap.destroy()
                    status_label.configure(text="解析出错", fg=DOWN_FG)
                    text_box.configure(state=tk.NORMAL)
                    text_box.delete("1.0", tk.END)
                    _insert_styled_analysis_text(text_box, f"解析出错: {err_msg}")
                    text_box.configure(state=tk.DISABLED)
            self.root.after(0, on_error)


def _dialog_row_from_committee_result(row: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Build a display-only row from the latest committee result."""
    out = dict(row or {})
    alloc = _safe_num(result.get("suggested_alloc_cny"))
    verdict = str(result.get("verdict") or "").upper()
    if alloc < 0 and verdict in {"", "HOLD", "WAIT"}:
        verdict = "TRIM"
    elif alloc > 0 and verdict in {"", "HOLD", "WAIT"}:
        verdict = "ACCUMULATE"
    status = _latest_committee_status_for_dialog(out, result, verdict, alloc)
    op = dict(out.get("operation") or {})
    op.update(
        {
            "status": status,
            "verdict": verdict or result.get("verdict") or op.get("verdict") or "HOLD",
            "confidence": _safe_num(result.get("confidence"), _safe_num(op.get("confidence"))),
            "suggested_alloc_cny": alloc,
        }
    )
    out["operation"] = op
    out["state"] = status
    out["decision_synthesis"] = result.get("decision_synthesis", out.get("decision_synthesis", {}))
    out["buy_signal_backtest"] = result.get("buy_signal_backtest", out.get("buy_signal_backtest", {}))
    if result.get("entry_exit_points"):
        ee = result.get("entry_exit_points") or {}
        out["buy_criteria"] = {
            "pullback_price": _safe_num(ee.get("buy_pullback_price")),
            "breakout_price": _safe_num(ee.get("buy_breakout_price")),
            "reentry_price": _safe_num(ee.get("reentry_price")),
            "reward_risk_ratio": _safe_num(ee.get("reward_risk_ratio")),
            "reason": ee.get("reason", ""),
        }
        out["entry_exit_points"] = ee
    if result.get("position_exit_policy"):
        out["position_exit_plan"] = result.get("position_exit_policy")
    if result.get("right_side_trend_gate"):
        out["right_side_trend_gate"] = result.get("right_side_trend_gate")
    if result.get("fundamental_score") is not None:
        fundamental = dict(out.get("fundamental") or {})
        fundamental.update(
            {
                "score": _safe_num(result.get("fundamental_score"), _safe_num(fundamental.get("score"), 50)),
                "model": result.get("fundamental_model", fundamental.get("model", "")),
                "anchor_multiplier": _safe_num(
                    result.get("fundamental_anchor_multiplier"),
                    _safe_num(fundamental.get("anchor_multiplier"), 1.0),
                ),
            }
        )
        out["fundamental"] = fundamental
    return out


