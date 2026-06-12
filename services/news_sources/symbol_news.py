"""Symbol-related news adapter.

This adapter keeps ticker-specific news coverage without depending on a market
data package. It delegates discovery to DDGS/web search and tags the results as
symbol news for downstream attribution.
"""
from __future__ import annotations

import logging
from typing import List

from services.news_sources import RawNewsItem

log = logging.getLogger(__name__)


def fetch_symbol_news(symbol: str, *, max_items: int = 20) -> List[RawNewsItem]:
    """Fetch symbol-related news through the generic web-news source."""
    try:
        from services.news_sources.ddgs_news import fetch_ddgs_news

        items = fetch_ddgs_news(
            query=f"{symbol} stock news",
            max_results=max_items,
            extract_fulltext=False,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("symbol news %s fetch failed: %s", symbol, e)
        return []

    for item in items:
        item.src_name = item.src_name.replace("ddgs", "symbol_news", 1)
        item.raw_meta.setdefault("symbol", symbol)
    return items
