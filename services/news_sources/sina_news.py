"""services/news_sources/sina_news.py —— 新浪财经（finance.sina.com.cn）新闻适配器

为什么用：
- 新浪财经滚动新闻是中文主流财经源，证券要闻 / 国际财经 / 市场评论 覆盖全
- feed.mix.sina.com.cn/api/roll/get 免签名、免 auth、实测稳定可直连
- 替代被墙/限流的 yfinance_news，且天然中文、贴 A股语境

接口（2026-05 实测）：
  GET https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=<LID>&num=N&page=1
  lid 频道：
    2516 = 证券要闻      2517 = 国际财经（覆盖中东地缘）
    2510 = 国内财经      2513 = 公司新闻
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List

import requests

from services.news_sources import RawNewsItem

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_ENDPOINT = "https://feed.mix.sina.com.cn/api/roll/get"
_TIMEOUT = 12

# 默认拉的频道（label 进 src_name，便于归一化时分辨来源）
DEFAULT_CHANNELS: List[Dict[str, str]] = [
    {"lid": "2516", "label": "证券要闻"},
    {"lid": "2517", "label": "国际财经"},
]


def fetch_sina_news(*, max_items: int = 20,
                    channels: List[Dict[str, str]] = None) -> List[RawNewsItem]:
    """拉新浪财经滚动新闻 → RawNewsItem 列表（多频道合并）。"""
    chans = channels or DEFAULT_CHANNELS
    per = max(1, max_items // max(1, len(chans)))
    items: List[RawNewsItem] = []
    for ch in chans:
        items.extend(_fetch_channel(ch["lid"], ch["label"], per))
    log.info(f"[sina_news] 合计 → {len(items)} 条 ({len(chans)} 频道)")
    return items


def _fetch_channel(lid: str, label: str, num: int) -> List[RawNewsItem]:
    url = (f"{_ENDPOINT}?pageid=153&lid={lid}&k=&num={min(num, 50)}"
           f"&page=1&r=0.1")
    try:
        r = requests.get(url, headers={"User-Agent": _UA,
                         "Referer": "https://finance.sina.com.cn"},
                         timeout=_TIMEOUT)
        r.raise_for_status()
        j = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning(f"[sina_news] {label}(lid={lid}) 抓取失败: {type(e).__name__}: {e}")
        return []

    data = (j.get("result") or {}).get("data") or []
    out: List[RawNewsItem] = []
    for it in data:
        title = (it.get("title") or "").strip()
        url_item = it.get("url") or ""
        if not title or not url_item:
            continue
        out.append(RawNewsItem(
            src_name=f"sina:{label}",
            title=title[:120],
            url=url_item,
            snippet=(it.get("intro") or it.get("summary") or "").strip()[:260],
            published_at=_epoch_to_iso(it.get("ctime") or it.get("intime")),
            raw_meta={"channel": label, "lid": lid, "source": "sina_finance"},
        ))
    log.info(f"[sina_news] {label} → {len(out)} 条")
    return out


def _epoch_to_iso(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OSError):
        return ""
