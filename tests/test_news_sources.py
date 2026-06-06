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
         patch("services.news_sources.yfinance_news.fetch_yfinance_news") as m_yf, \
         patch("services.news_sources.rss_feed.fetch_rss") as m_rss:
        m_ddgs.return_value = _stub_items("ddgs", 3)
        m_yf.return_value = _stub_items("yf", 2)
        m_rss.return_value = [
            RawNewsItem(src_name="rss:r", title="t", url="https://ddgs.example/0", snippet="dup"),
            RawNewsItem(src_name="rss:r", title="t", url="https://rss.example/0", snippet="x"),
        ]
        out = fetch_all(
            queries=["foo"],
            symbols=["NDQ.AX"],
            rss_feeds=[{"name": "r", "url": "https://feed"}],
            domestic=False,
        )
    # 3 ddgs + 2 yf + 2 rss = 7 raw，1 条 url 跟 ddgs/0 重复 → 6 条
    assert len(out) == 6
    assert m_ddgs.called and m_yf.called and m_rss.called


def test_fetch_all_continues_on_per_source_failure():
    with patch("services.news_sources.ddgs_news.fetch_ddgs_news",
               side_effect=RuntimeError("boom")), \
         patch("services.news_sources.yfinance_news.fetch_yfinance_news") as m_yf, \
         patch("services.news_sources.rss_feed.fetch_rss") as m_rss:
        m_yf.return_value = _stub_items("yf", 2)
        m_rss.return_value = _stub_items("rss", 1)
        out = fetch_all(
            queries=["x"], symbols=["A"],
            rss_feeds=[{"name": "r", "url": "https://feed"}],
            domestic=False,
        )
    # ddgs 挂了不影响其他
    assert len(out) == 3


def test_fetch_all_empty_inputs_without_domestic_sources():
    assert fetch_all(domestic=False) == []


def test_domestic_hot_news_infers_sector_and_leaders():
    from services.news_sources.domestic_hot_news import (
        enrich_news_items,
        format_sector_brief,
        summarize_sector_hits,
    )

    items = enrich_news_items([
        RawNewsItem(
            src_name="stub",
            title="国产大模型推动AI算力服务器需求提升",
            url="https://n.example/1",
            snippet="光模块、PCB和数据中心产业链受关注",
        )
    ])

    sectors = items[0].raw_meta["sectors"]
    assert sectors[0]["sector"] == "AI算力"
    assert any(leader["symbol"] == "601138" for leader in sectors[0]["leaders"])

    brief = format_sector_brief(items)
    assert "涉及板块: AI算力" in brief
    assert "工业富联(601138)" in brief

    summary = summarize_sector_hits(items)
    assert summary[0]["sector"] == "AI算力"
    assert summary[0]["count"] == 1


def test_fetch_all_domestic_includes_hot_news_and_enrichment():
    with patch("services.news_sources.domestic_news.fetch_eastmoney_news", return_value=[]), \
         patch("services.news_sources.domestic_news.fetch_cls_news", return_value=[]), \
         patch("services.news_sources.domestic_news.fetch_sina_news", return_value=[]), \
         patch("services.news_sources.domestic_news.fetch_xueqiu_news", return_value=[]), \
         patch("services.news_sources.domestic_news.fetch_wallstreetcn_news", return_value=[]), \
         patch("services.news_sources.domestic_hot_news.fetch_baidu_hot_news") as m_hot:
        m_hot.return_value = [
            RawNewsItem(
                src_name="baidu_hot",
                title="人形机器人订单增长",
                url="https://hot.example/1",
                snippet="减速器和伺服产业链升温",
            )
        ]
        out = fetch_all(domestic=True, max_per_source=2, timeout_sec=5)

    assert len(out) == 1
    assert out[0].src_name == "baidu_hot"
    assert out[0].raw_meta["sectors"][0]["sector"] == "机器人"


def test_obvious_danger_news_gets_quantified_a_share_impact():
    from services.news_sources.news_risk_model import (
        assess_news_risk,
        enrich_risk_impacts,
        format_risk_impact_brief,
    )

    item = RawNewsItem(
        src_name="sina",
        title="中东军事冲突升级，多地遭导弹袭击，全球市场避险升温",
        url="https://risk.example/1",
        snippet="冲突导致油价大幅波动，投资者担忧全球供应链风险。",
    )

    impacts = assess_news_risk(item)
    assert impacts
    top = impacts[0]
    assert top["category"] == "地缘冲突/战争"
    assert isinstance(top["csi300_impact_bps"], float)
    assert top["csi300_impact_bps"] < -20
    assert len(top["ci90_bps"]) == 2
    assert top["ci90_bps"][0] < top["csi300_impact_bps"] < top["ci90_bps"][1]
    assert top["sector_impacts"]
    assert top["model"]["formula"] == "-base_bps(category) * severity * source_reliability * a_share_proximity"
    assert "军事冲突" in top["evidence_keywords"]

    enriched = enrich_risk_impacts([item])
    assert enriched[0].raw_meta["risk_impacts"][0]["category"] == "地缘冲突/战争"
    brief = format_risk_impact_brief(enriched)
    assert "CSI300" in brief
    assert "90%区间" in brief


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
