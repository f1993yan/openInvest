"""Constants and theme tokens for the desktop monitor window."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "data" / "market_monitor" / "latest_window.json"
WEEKEND_NEWS_DIR = ROOT / "data" / "weekend_news"
DAILY_SELECTION_LATEST = ROOT / "data" / "daily_stock_selection" / "latest.json"
BACKGROUND_PROCESS_PATTERNS = (
    "jobs.market_monitor",
    "scheduler.runner",
    "jobs.weekend_news_crawl",
    "jobs.daily_stock_selection",
    "scripts.daily_stock_selection",
)
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
STOCK_CARD_STAGE_HEIGHT = 144
NEWS_CARD_STAGE_HEIGHT = 250

STATE_LABELS = {
    "action_required": "需要操作",
    "trigger_confirmed": "触发确认",
    "watch_trigger": "单轮触发",
    "candidate": "候选",
    "monitoring": "监控中",
    "executed": "已执行",
    "blocked": "已拦截",
    "error": "错误",
}

STATE_SIGNALS = {
    "action_required": "操作",
    "trigger_confirmed": "触发",
    "watch_trigger": "预警",
    "candidate": "候选",
    "monitoring": "观察",
    "executed": "已执行",
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

STATE_PRIORITY = {
    "action_required": 0,
    "trigger_confirmed": 1,
    "watch_trigger": 2,
    "candidate": 3,
    "monitoring": 4,
    "executed": 5,
    "blocked": 6,
    "error": 7,
}

VERDICT_PRIORITY = {
    "SELL": 0,
    "TRIM": 1,
    "BUY": 2,
    "ACCUMULATE": 3,
    "HOLD": 4,
    "WAIT": 5,
}




__all__ = [
    "ROOT",
    "DEFAULT_SNAPSHOT",
    "WEEKEND_NEWS_DIR",
    "DAILY_SELECTION_LATEST",
    "BACKGROUND_PROCESS_PATTERNS",
    "BOARD_BG",
    "PANEL_BG",
    "HEADER_BG",
    "HEADER_FG",
    "TEXT",
    "MUTED",
    "LINE",
    "CARD_BG",
    "CARD_ACTIVE_BG",
    "BLUE",
    "SOFT_BLUE",
    "ACTION_BG",
    "ACTION_FG",
    "TRIGGER_BG",
    "TRIGGER_FG",
    "UP_FG",
    "DOWN_FG",
    "STOCK_CARD_STAGE_HEIGHT",
    "NEWS_CARD_STAGE_HEIGHT",
    "STATE_LABELS",
    "STATE_SIGNALS",
    "VERDICT_ARROWS",
    "STATE_PRIORITY",
    "VERDICT_PRIORITY",
]
