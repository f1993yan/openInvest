"""国内新闻源聚合

已验证 (全部通过 Playwright 或 HTTP):
  - 东方财富 (akshare stock_news_em)
  - 财联社 (Playwright 拦截 /api/cache)
  - 新浪财经 (feed.mix.sina.com.cn API)
  - 雪球 (Playwright 拦截 fundx/public/list.json)
  - 华尔街见闻 (api-one.wallstcn.com)

依赖: pip install playwright && playwright install chromium

每个源独立降级，任一失败不影响其他源。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List

import requests

from services.news_sources import RawNewsItem

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ==========================================
# 东方财富
# ==========================================

def fetch_eastmoney_news(*, max_items: int = 20) -> List[RawNewsItem]:
    items: List[RawNewsItem] = []
    now = _now()
    try:
        import akshare as ak
        df = ak.stock_news_em()
        if df is not None and not df.empty:
            for _, row in df.head(max_items).iterrows():
                items.append(RawNewsItem(
                    src_name="eastmoney",
                    title=str(row.get("新闻标题", "")),
                    url=str(row.get("新闻链接", "")),
                    snippet=str(row.get("新闻内容", ""))[:300],
                    published_at=str(row.get("发布时间", "")),
                    fetched_at=now,
                ))
    except Exception as e:
        log.warning(f"eastmoney akshare 失败: {e}")
    return items


# ==========================================
# 财联社 (RSS: www.cls.cn)
# ==========================================

def fetch_cls_news(*, max_items: int = 20) -> List[RawNewsItem]:
    """财联社电报 — 通过 Playwright 拦截 /api/cache 获取 roll_data。

    需要: pip install playwright && playwright install chromium
    """
    items: List[RawNewsItem] = []
    now = _now()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
            )
            ctx = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
                locale='zh-CN',
            )
            page = ctx.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "window.chrome = { runtime: {} };"
            )

            telegraph_items = []

            def _on_response(resp):
                if 'api/cache' in resp.url and 'telegraph' in resp.url:
                    try:
                        d = resp.json()
                        roll = d.get('data', {}).get('roll_data', [])
                        telegraph_items.extend(roll)
                    except Exception:
                        pass

            page.on('response', _on_response)
            page.goto('https://www.cls.cn/telegraph', wait_until='networkidle', timeout=30000)
            page.wait_for_timeout(2000)
            browser.close()

            for entry in telegraph_items[:max_items]:
                title = entry.get('title', '') or ''
                brief = entry.get('brief', '') or entry.get('content', '')
                ctime = entry.get('ctime', 0)
                if not title and not brief:
                    continue
                items.append(RawNewsItem(
                    src_name="cls",
                    title=title if title else brief[:80],
                    url=f"https://www.cls.cn/detail/{entry.get('id', '')}",
                    snippet=brief[:300],
                    published_at=datetime.fromtimestamp(ctime).isoformat() if ctime else None,
                    fetched_at=now,
                ))
    except ImportError:
        log.warning("cls 财联社: playwright 未安装，跳过。安装: pip install playwright && playwright install chromium")
    except Exception as e:
        log.warning(f"cls 财联社失败: {e}")
    return items
    # --- 以下为旧实现（保留作参考）---
    try:
        resp = requests.get(
            "https://www.cls.cn/telegraph",
            headers={"User-Agent": _UA, "Referer": "https://www.cls.cn/"},
            timeout=15,
        )
        resp.encoding = "utf-8"
        # 从页面提取 <script id="__NEXT_DATA__"> JSON
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
        if match:
            import json as _json
            data = _json.loads(match.group(1))
            telegraph = (
                data.get("props", {})
                .get("pageProps", {})
                .get("telegraphList", [])
            )
            for entry in telegraph[:max_items]:
                items.append(RawNewsItem(
                    src_name="cls",
                    title=entry.get("title", "")[:100],
                    url=f"https://www.cls.cn/detail/{entry.get('id', '')}",
                    snippet=entry.get("content", "") or entry.get("brief", ""),
                    published_at=datetime.fromtimestamp(
                        entry.get("ctime", 0)
                    ).isoformat() if entry.get("ctime") else None,
                    fetched_at=now,
                ))
    except Exception as e:
        log.warning(f"cls 财联社失败: {e}")
    return items


# ==========================================
# 新浪财经
# ==========================================

def fetch_sina_news(*, max_items: int = 20) -> List[RawNewsItem]:
    items: List[RawNewsItem] = []
    now = _now()
    try:
        resp = requests.get(
            "https://feed.mix.sina.com.cn/api/roll/get",
            params={"pageid": 153, "lid": 2509, "num": max_items},
            headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"},
            timeout=15,
        )
        data = resp.json()
        for entry in data.get("result", {}).get("data", [])[:max_items]:
            items.append(RawNewsItem(
                src_name="sina",
                title=entry.get("title", ""),
                url=entry.get("url", ""),
                snippet=entry.get("intro", "") or entry.get("summary", ""),
                published_at=entry.get("ctime"),
                fetched_at=now,
            ))
    except Exception as e:
        log.warning(f"sina 新浪失败: {e}")
    return items


# ==========================================
# 雪球
# ==========================================

def _xueqiu_cookies() -> dict:
    try:
        s = requests.Session()
        s.get("https://xueqiu.com/", headers={"User-Agent": _UA}, timeout=10)
        return dict(s.cookies)
    except Exception:
        return {}


def fetch_xueqiu_news(*, query: str = "A股", max_items: int = 20) -> List[RawNewsItem]:
    """雪球热门讨论 — 通过 Playwright 拦截 fundx/public/list.json。

    需要: pip install playwright && playwright install chromium
    """
    items: List[RawNewsItem] = []
    now = _now()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
            )
            ctx = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
                locale='zh-CN',
            )
            page = ctx.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "window.chrome = { runtime: {} };"
            )

            posts = []

            def _on_response(resp):
                if 'fundx/public/list.json' in resp.url or 'statuses/hot' in resp.url:
                    try:
                        d = resp.json()
                        lst = d.get('list', []) or d.get('data', {}).get('list', [])
                        if isinstance(lst, list):
                            posts.extend(lst)
                    except Exception:
                        pass

            page.on('response', _on_response)
            page.goto('https://xueqiu.com/', wait_until='networkidle', timeout=30000)
            page.wait_for_timeout(2000)
            browser.close()

            for entry in posts[:max_items]:
                title = entry.get('title', '') or entry.get('text', '') or entry.get('description', '')
                if not title:
                    continue
                # 去掉HTML标签
                import re
                title = re.sub(r'<[^>]+>', '', title).strip()
                items.append(RawNewsItem(
                    src_name="xueqiu",
                    title=title[:100],
                    url=f"https://xueqiu.com{entry.get('target','')}" if entry.get('target') else '',
                    snippet=entry.get('text', '') or entry.get('description', ''),
                    published_at=datetime.fromtimestamp(
                        entry.get('created_at', 0) / 1000
                    ).isoformat() if entry.get('created_at') else None,
                    fetched_at=now,
                ))
    except ImportError:
        log.warning("xueqiu 雪球: playwright 未安装，跳过")
    except Exception as e:
        log.warning(f"xueqiu 雪球失败: {e}")
    return items
    # --- 以下为旧实现（保留作参考）---
    try:
        cookies = _xueqiu_cookies()
        resp = requests.get(
            "https://xueqiu.com/query/v1/search/web/search.json",
            params={"q": query, "page": 1, "size": max_items},
            headers={"User-Agent": _UA, "Referer": "https://xueqiu.com/"},
            cookies=cookies,
            timeout=15,
        )
        data = resp.json()
        for entry in data.get("list", [])[:max_items]:
            items.append(RawNewsItem(
                src_name=f"xueqiu:{query}",
                title=entry.get("title", "") or entry.get("name", ""),
                url=f"https://xueqiu.com{entry.get('target', '')}",
                snippet=entry.get("description", "") or entry.get("text", "")[:200],
                fetched_at=now,
            ))
    except Exception as e:
        log.warning(f"xueqiu 失败: {e}")
    return items


# ==========================================
# 华尔街见闻
# ==========================================

def fetch_wallstreetcn_news(*, max_items: int = 20) -> List[RawNewsItem]:
    items: List[RawNewsItem] = []
    now = _now()
    try:
        resp = requests.get(
            "https://api-one.wallstcn.com/apiv1/content/lives",
            params={"channel": "global-channel", "client": "pc", "limit": max_items},
            headers={"User-Agent": _UA, "Referer": "https://wallstreetcn.com/"},
            timeout=15,
        )
        data = resp.json()
        for entry in data.get("data", {}).get("items", [])[:max_items]:
            resource = entry.get("resource", {})
            content = resource.get("content_text", "") or resource.get("title", "")
            uri = resource.get("uri", "")
            items.append(RawNewsItem(
                src_name="wallstreetcn",
                title=resource.get("title", "") or content[:80],
                url=f"https://wallstreetcn.com{uri}" if uri else "",
                snippet=content[:300],
                published_at=entry.get("display_time"),
                fetched_at=now,
            ))
    except Exception as e:
        log.warning(f"wallstreetcn 失败: {e}")
    return items
