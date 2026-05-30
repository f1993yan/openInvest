"""services/news_sources 单测 —— mock 三个源的底层调用，验证统一入口 + 去重"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.news_sources import RawNewsItem, fetch_all


def _stub_items(prefix: str, n: int):
    return [
        RawNewsItem(
            src_name=f"stub:{prefix}", title=f"{prefix} t{i}",
            url=f"https://{prefix}.example/{i}", snippet="x",
        )
        for i in range(n)
    ]


def test_fetch_all_runs_each_source_and_dedups_urls():
    with patch("services.news_sources.ddgs_news.fetch_ddgs_news") as m_ddgs, \
         patch("services.news_sources.cls_news.fetch_cls_telegraph") as m_cls, \
         patch("services.news_sources.sina_news.fetch_sina_news") as m_sina, \
         patch("services.news_sources.rss_feed.fetch_rss") as m_rss:
        m_ddgs.return_value = _stub_items("ddgs", 3)
        m_cls.return_value = _stub_items("cls", 2)
        m_sina.return_value = _stub_items("sina", 2)
        m_rss.return_value = [
            RawNewsItem(src_name="rss:r", title="t", url="https://ddgs.example/0", snippet="dup"),
            RawNewsItem(src_name="rss:r", title="t", url="https://rss.example/0", snippet="x"),
        ]
        out = fetch_all(
            queries=["foo"],
            symbols=["NDQ.AX"],   # symbols 已废弃但保留兼容，应被忽略
            rss_feeds=[{"name": "r", "url": "https://feed"}],
        )
    # 3 ddgs + 2 cls + 2 sina + 2 rss = 9 raw，1 条 url 跟 ddgs/0 重复 → 8 条
    assert len(out) == 8
    assert m_ddgs.called and m_cls.called and m_sina.called and m_rss.called


def test_fetch_all_continues_on_per_source_failure():
    with patch("services.news_sources.ddgs_news.fetch_ddgs_news",
               side_effect=RuntimeError("boom")), \
         patch("services.news_sources.cls_news.fetch_cls_telegraph") as m_cls, \
         patch("services.news_sources.sina_news.fetch_sina_news") as m_sina, \
         patch("services.news_sources.rss_feed.fetch_rss") as m_rss:
        m_cls.return_value = _stub_items("cls", 2)
        m_sina.return_value = _stub_items("sina", 2)
        m_rss.return_value = _stub_items("rss", 1)
        out = fetch_all(
            queries=["x"], symbols=["A"],
            rss_feeds=[{"name": "r", "url": "https://feed"}],
        )
    # ddgs 挂了不影响其他：2 cls + 2 sina + 1 rss = 5
    assert len(out) == 5


def test_fetch_all_cn_sources_run_without_queries():
    """CLS + 新浪是实时财经源，不依赖 query/rss，每轮都应触发。"""
    with patch("services.news_sources.cls_news.fetch_cls_telegraph") as m_cls, \
         patch("services.news_sources.sina_news.fetch_sina_news") as m_sina:
        m_cls.return_value = _stub_items("cls", 1)
        m_sina.return_value = _stub_items("sina", 1)
        out = fetch_all()   # 无 queries / 无 rss
    assert len(out) == 2
    assert m_cls.called and m_sina.called


def test_rss_feed_parses_minimal_feed():
    """单测 rss_feed.fetch_rss 自身 —— mock feedparser.parse"""
    from services.news_sources.rss_feed import fetch_rss
    fake = MagicMock()
    fake.entries = [
        {"link": "https://r.com/a", "title": "<b>A</b>",
         "summary": "<p>hello world</p>", "published_parsed": (2026, 5, 13, 10, 0, 0, 0, 0, 0)},
        {"link": "", "title": "skip me"},  # 无 link 跳过
    ]
    with patch("feedparser.parse", return_value=fake):
        out = fetch_rss("test", "https://feed", max_items=10)
    assert len(out) == 1
    assert out[0].title == "<b>A</b>"  # title 不剥 HTML（让 LLM 看到原貌）
    assert "<p>" not in out[0].snippet   # snippet 剥了
    assert out[0].published_at and out[0].published_at.startswith("2026-05-13")
