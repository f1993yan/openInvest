"""Notification timing and Windows toast helpers for market monitor."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from jobs.market_monitor_common import TRADING_END

def send_windows_toast(title: str, body: str):
    """Windows 通知 — 使用 ctypes MessageBox（非阻塞线程）"""
    import ctypes
    import threading

    def _show():
        try:
            ctypes.windll.user32.MessageBoxW(
                0, body, title,
                0x40 | 0x40000,  # MB_ICONINFORMATION | MB_TOPMOST
            )
        except Exception:
            pass

    threading.Thread(target=_show, daemon=True).start()

def is_scheduled_monitor_popup_time(now: Optional[datetime] = None) -> bool:
    """Only scheduled summary times may popup for news/no-action updates."""
    now = now or datetime.now()
    if now.hour == TRADING_END.hour and now.minute == TRADING_END.minute:
        return True
    return now.minute in (0, 30)

def should_send_monitor_summary_popup(
    *,
    now: Optional[datetime] = None,
    actionable: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Send a summary popup on scheduled times or when trade action is needed."""
    return is_scheduled_monitor_popup_time(now) or bool(actionable)
