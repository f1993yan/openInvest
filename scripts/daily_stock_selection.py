"""Run daily A-share stock selection.

This command builds a deterministic daily watchlist from domestic news and
A-share OHLCV behavior. It writes JSON under data/daily_stock_selection/ by
default; the data directory is intentionally local-only.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

from core.daily_stock_selector import build_daily_selection, result_to_dict
from services.news_sources import RawNewsItem, fetch_all
from services.news_sources.domestic_hot_news import enrich_news_items
from utils.akshare_data import get_history_data


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "daily_stock_selection"


def run_daily_stock_selection(
    *,
    trade_date: str | None = None,
    max_news: int = 30,
    max_stocks: int = 20,
    write_file: bool = True,
    available_cash_cny: float | None = None,
    use_portfolio_cash: bool = True,
) -> Dict[str, Any]:
    items = fetch_all(
        queries=["A股 热点 板块", "今日 A股 题材"],
        domestic=True,
        max_per_source=max_news,
        extract_fulltext=False,
        timeout_sec=35,
    )
    enriched = enrich_news_items(items)
    history = _load_candidate_history(enriched)
    cash_constraint = available_cash_cny
    if cash_constraint is None and use_portfolio_cash:
        cash_constraint = _load_portfolio_cash_cny()
    result = build_daily_selection(
        enriched,
        history,
        trade_date=trade_date,
        max_stocks=max_stocks,
        available_cash_cny=cash_constraint,
    )
    payload = result_to_dict(result)
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["source_news_count"] = len(enriched)
    if write_file:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        date_part = payload.get("trade_date") or datetime.now().strftime("%Y-%m-%d")
        out_path = OUT_DIR / f"selection_{date_part}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["output_path"] = str(out_path)
    return payload


def _load_portfolio_cash_cny() -> float | None:
    try:
        from core.portfolio_manager import PortfolioManager

        return PortfolioManager().cash_amount("CNY")
    except Exception:
        return None


def _load_candidate_history(items: Iterable[RawNewsItem]) -> Dict[str, Any]:
    symbols = _candidate_symbols(items)
    history: Dict[str, Any] = {}
    for symbol in symbols:
        try:
            df = get_history_data(symbol, "3mo")
        except Exception:
            continue
        if df is not None and not df.empty:
            history[symbol] = df
    return history


def _candidate_symbols(items: Iterable[RawNewsItem]) -> List[str]:
    out: List[str] = []
    for item in items:
        for sector in (item.raw_meta or {}).get("sectors", []) or []:
            for leader in sector.get("leaders", []) or []:
                symbol = str(leader.get("symbol") or "").strip()
                if _is_a_share(symbol) and symbol not in out:
                    out.append(symbol)
    return out


def _is_a_share(symbol: str) -> bool:
    return symbol.isdigit() and len(symbol) == 6 and symbol[0] in {"0", "2", "3", "4", "6", "8"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build daily A-share stock selection JSON")
    parser.add_argument("--date", dest="trade_date", help="Trade date YYYY-MM-DD; defaults to latest bar")
    parser.add_argument("--max-news", type=int, default=30)
    parser.add_argument("--max-stocks", type=int, default=20)
    parser.add_argument("--cash-cny", type=float, default=None, help="Override CNY cash constraint")
    parser.add_argument("--ignore-cash", action="store_true", help="Do not filter by portfolio cash")
    parser.add_argument("--no-write", action="store_true", help="Print JSON only; do not write data/")
    args = parser.parse_args()
    payload = run_daily_stock_selection(
        trade_date=args.trade_date,
        max_news=args.max_news,
        max_stocks=args.max_stocks,
        write_file=not args.no_write,
        available_cash_cny=args.cash_cny,
        use_portfolio_cash=not args.ignore_cash,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
