"""services/news_sources/cls_news.py —— 财联社（cls.cn）电报适配器

为什么用：
- 财联社电报是 A股盘中最快的中文实时资讯（财经/地缘/个股/板块），
  比 ddgs/rss 时效性强，且天然中文、贴 A股语境
- 自带 subjects（题材标签，如"中东冲突""人工智能"）和 stock_list（关联个股），
  给 event_normalizer 归一化提供高质量结构化线索

接口（2026-05 实测可用）：
  GET https://www.cls.cn/v1/roll/get_roll_list
  签名 sign = md5(sha1(sorted_urlencode(params)))   ← 老的 /nodeapi/updateTelegraphList 已 404
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import List
from urllib.parse import urlencode

import requests

from services.news_sources import RawNewsItem

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_ENDPOINT = "https://www.cls.cn/v1/roll/get_roll_list"
_TIMEOUT = 12


def _sign(params: dict) -> str:
    """财联社 web 签名：md5(sha1(按 key 排序的 urlencode))。"""
    q = urlencode(dict(sorted(params.items())))
    sha1 = hashlib.sha1(q.encode()).hexdigest()
    return hashlib.md5(sha1.encode()).hexdigest()


def fetch_cls_telegraph(*, max_items: int = 20, category: str = "") -> List[RawNewsItem]:
    """拉财联社电报最新条目 → RawNewsItem 列表。

    Args:
        max_items: 最多返回多少条
        category:  ""=全部；可传 "red"（重要/红色电报）等财联社分类
    """
    params = {
        "app": "CailianpressWeb",
        "os": "web",
        "sv": "7.7.5",
        "category": category,
        "rn": str(min(max(max_items, 1), 50)),
        "last_time": "",
    }
    params["sign"] = _sign(params)
    url = _ENDPOINT + "?" + urlencode(params)
    try:
        r = requests.get(url, headers={"User-Agent": _UA,
                         "Referer": "https://www.cls.cn/telegraph"},
                         timeout=_TIMEOUT)
        r.raise_for_status()
        j = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning(f"[cls_news] 抓取失败: {type(e).__name__}: {e}")
        return []

    if j.get("errno") not in (0, None):
        log.warning(f"[cls_news] errno={j.get('errno')} msg={j.get('msg')}")
        return []

    roll = (j.get("data") or {}).get("roll_data") or []
    items: List[RawNewsItem] = []
    for it in roll[:max_items]:
        if it.get("is_ad") or it.get("type") == 1:   # 跳过广告
            continue
        title = (it.get("title") or "").strip()
        content = (it.get("content") or it.get("brief") or "").strip()
        if not title:
            # 电报常无独立 title，用 content 头部当标题
            title = content[:40]
        if not (title or content):
            continue
        cid = it.get("id")
        url_item = it.get("shareurl") or (
            f"https://www.cls.cn/detail/{cid}" if cid else "")
        if not url_item:
            continue
        subjects = [s.get("subject_name") for s in (it.get("subjects") or [])
                    if s.get("subject_name")]
        stocks = [s.get("name") for s in (it.get("stock_list") or [])
                  if s.get("name")]
        items.append(RawNewsItem(
            src_name="cls:电报",
            title=title[:120],
            url=url_item,
            snippet=content[:260],
            published_at=_epoch_to_iso(it.get("ctime")),
            raw_meta={
                "level": it.get("level"),       # A=重要红头条
                "subjects": subjects,
                "stocks": stocks,
                "source": "cailianpress",
            },
        ))
    log.info(f"[cls_news] 电报 → {len(items)} 条")
    return items


def _epoch_to_iso(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OSError):
        return ""
