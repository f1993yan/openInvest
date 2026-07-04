"""Shared helpers for the market monitor modules."""
from __future__ import annotations

import json
import logging
import math
import os
import sys
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass

REPORT_DIR = _PROJECT_ROOT / "data" / "market_monitor"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("market_monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [market_monitor] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(REPORT_DIR / "monitor.log", encoding="utf-8"),
    ],
)

from datetime import time as dt_time

CONFIG_PATH = Path(__file__).with_name("market_monitor_config.json")
TRADING_START = dt_time(9, 30)
LUNCH_START = dt_time(11, 30)
LUNCH_END = dt_time(13, 0)
TRADING_END = dt_time(15, 0)

class DynamicInterval:
    def __int__(self): return self.get_value()
    def __index__(self): return self.get_value()
    def __repr__(self): return str(self.get_value())
    def __add__(self, other): return self.get_value() + other
    def __radd__(self, other): return other + self.get_value()
    def __mul__(self, other): return self.get_value() * other
    def __rmul__(self, other): return other * self.get_value()
    def __truediv__(self, other): return self.get_value() / other
    def __rtruediv__(self, other): return other / self.get_value()
    def __floordiv__(self, other): return self.get_value() // other
    def __rfloordiv__(self, other): return other // self.get_value()
    def __lt__(self, other): return self.get_value() < other
    def __le__(self, other): return self.get_value() <= other
    def __eq__(self, other): return self.get_value() == other
    def __ne__(self, other): return self.get_value() != other
    def __gt__(self, other): return self.get_value() > other
    def __ge__(self, other): return self.get_value() >= other

    def get_value(self) -> int:
        try:
            settings_path = _PROJECT_ROOT / "jobs" / "crawler_settings.json"
            if settings_path.exists():
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                val = int(data.get("frequency_minutes", 60))
                return max(1, val)
        except Exception:
            pass
        return max(1, int(os.getenv("INVEST_MONITOR_INTERVAL_MINUTES", "10")))


def load_crawler_settings() -> Dict[str, Any]:
    settings_path = _PROJECT_ROOT / "jobs" / "crawler_settings.json"
    defaults = {
        "frequency_minutes": 60,
        "target_refresh_enabled": True,
        "news_refresh_enabled": True
    }
    if not settings_path.exists():
        return defaults
    try:
        return {**defaults, **json.loads(settings_path.read_text(encoding="utf-8"))}
    except Exception:
        return defaults


INTERVAL_MINUTES = DynamicInterval()
ENTRY_EXIT_ALERT_STATE_PATH = REPORT_DIR / "entry_exit_alert_state.json"
LATEST_WINDOW_PATH = REPORT_DIR / "latest_window.json"
ACTION_EMAIL_STATE_PATH = REPORT_DIR / "action_email_state.json"
MONITOR_POPUPS_ENABLED = os.getenv("INVEST_MONITOR_POPUPS", "0") == "1"
MONITOR_ACTION_EMAILS_ENABLED = os.getenv("INVEST_MONITOR_EMAIL_ACTIONS", "0") == "1"
AUTO_TRADE_REPEAT_COOLDOWN_MINUTES = max(
    0,
    int(os.getenv("INVEST_AUTO_TRADE_REPEAT_COOLDOWN_MINUTES", "60")),
)
COST_STOP_LOSS_PCT = max(
    0.0,
    float(os.getenv("INVEST_MONITOR_COST_STOP_LOSS_PCT", "12")),
)
POSITION_EXIT_PLAN_VERSION = 1

def _safe_num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _fmt_price(value: Any) -> str:
    v = _safe_num(value, 0.0)
    return f"{v:.2f}" if v > 0 else "-"


def _round_trade_price(value: float) -> float:
    return round(max(_safe_num(value), 0.0), 2)

__all__ = [
    "_PROJECT_ROOT", "REPORT_DIR", "CONFIG_PATH", "TRADING_START", "LUNCH_START",
    "LUNCH_END", "TRADING_END", "INTERVAL_MINUTES", "ENTRY_EXIT_ALERT_STATE_PATH",
    "LATEST_WINDOW_PATH", "ACTION_EMAIL_STATE_PATH", "MONITOR_POPUPS_ENABLED",
    "MONITOR_ACTION_EMAILS_ENABLED", "AUTO_TRADE_REPEAT_COOLDOWN_MINUTES",
    "COST_STOP_LOSS_PCT", "POSITION_EXIT_PLAN_VERSION", "log", "_safe_num", "_clamp",
    "_fmt_price", "_round_trade_price",
]
