"""Scheduled daily A-share stock selection."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.daily_stock_selection import run_daily_stock_selection
from utils.market_calendar import closed_reason, is_trading_day


def run() -> dict:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if not is_trading_day("XSHG", now.date()):
        return {
            "status": "skipped",
            "reason": closed_reason("XSHG", now.date()),
            "generated_at": now.isoformat(timespec="seconds"),
        }
    result = run_daily_stock_selection(write_file=True)
    return {
        "status": "ok",
        "trade_date": result.get("trade_date"),
        "stocks": len(result.get("stocks") or []),
        "output_path": result.get("output_path"),
        "latest_path": result.get("latest_path"),
    }
