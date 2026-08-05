"""services/news_sources 单测 —— mock 三个源的底层调用，验证统一入口 + 去重"""
from __future__ import annotations

import sys
import types
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
         patch("services.news_sources.symbol_news.fetch_symbol_news") as m_symbol, \
         patch("services.news_sources.rss_feed.fetch_rss") as m_rss:
        m_ddgs.return_value = _stub_items("ddgs", 3)
        m_symbol.return_value = _stub_items("symbol", 2)
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
    assert m_ddgs.called and m_symbol.called and m_rss.called


def test_fetch_all_continues_on_per_source_failure():
    with patch("services.news_sources.ddgs_news.fetch_ddgs_news",
               side_effect=RuntimeError("boom")), \
         patch("services.news_sources.symbol_news.fetch_symbol_news") as m_symbol, \
         patch("services.news_sources.rss_feed.fetch_rss") as m_rss:
        m_symbol.return_value = _stub_items("symbol", 2)
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
         patch("services.news_sources.cls_news.fetch_cls_telegraph", return_value=[]), \
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


def test_weekend_llm_summary_discovers_a_share_hot_opportunities(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from jobs import weekend_news_crawl as mod

    monkeypatch.setattr(mod, "CACHE_DIR", tmp_path)

    payload = {
        "key_themes": ["商业航天"],
        "theme_detail": {"商业航天": "SpaceX上市预期带动商业航天产业链关注。"},
        "sector_opportunities": [
            {
                "theme": "商业航天",
                "sector": "商业航天",
                "logic": "海外商业航天融资热度提升 -> A股卫星制造和航天材料映射",
                "heat_score": 0.82,
                "freshness_score": 0.76,
                "evidence_titles": ["SpaceX即将上市带动商业航天概念"],
                "leaders": [
                    {
                        "symbol": "603308",
                        "name": "应流股份",
                        "leader_type": "事件受益标的",
                        "reason": "高端铸件覆盖航空航天供应链",
                        "confidence": 0.68,
                        "risk_note": "海外公司上市到A股映射存在兑现风险",
                        "evidence_titles": ["SpaceX即将上市带动商业航天概念"],
                    }
                ],
            }
        ],
        "hot_stock_opportunities": [
            {
                "rank": 1,
                "symbol": "603308",
                "name": "应流股份",
                "sector": "商业航天",
                "theme": "商业航天",
                "score": 0.79,
                "reason": "商业航天热度提升，A股高端制造映射清晰",
                "risk_note": "题材追高风险",
            }
        ],
        "watchlist_symbols": ["603308", "00700"],
        "rejected_topics": [],
        "overall_sentiment": "positive",
        "summary_one_liner": "商业航天成为周末最强题材之一。",
    }

    class FakeCompletions:
        def create(self, **kwargs):
            assert "不是评估用户已有持仓" in kwargs["messages"][1]["content"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=f"```json\n{__import__('json').dumps(payload, ensure_ascii=False)}\n```")
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("utils.llm.get_llm_config_safe", lambda: ("fake-key", "https://example.test", "fake-model", "openai"))
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    caches = [
        {
            "slot": "2026-06-07_Sun_2000",
            "total_items": 1,
            "items": [
                {
                    "src_name": "baidu_hot",
                    "title": "SpaceX即将上市带动商业航天概念",
                    "snippet": "卫星制造、航天材料和高端装备产业链受关注",
                    "url": "https://n.example/1",
                }
            ],
        }
    ]

    out = mod.summarize_and_evaluate(caches, {"holdings": [{"symbol": "00700", "position_pct": 30}]})

    assert out["hot_stock_opportunities"][0]["symbol"] == "603308"
    assert out["sector_opportunities"][0]["sector"] == "商业航天"
    assert out["watchlist_symbols"] == ["603308"]
    assert out["need_committee_rerun"] == []
    assert "stock_impact" not in out
    assert out["_source_date_range"] == "2026-06-07"


def test_weekend_committee_on_leaders_uses_direct_backend_call(monkeypatch):
    from jobs import weekend_news_crawl as mod

    captured = {}

    class _FakeHolding:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _FakeRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _FakeResponse:
        def model_dump(self):
            return {
                "success": True,
                "symbol": "600900",
                "verdict": "ACCUMULATE",
                "confidence": 0.7,
                "suggested_alloc_cny": 3000,
                "cio_memo": "ok",
            }

    def _fake_direct(req):
        captured["req"] = req
        return _FakeResponse()

    def _fail_http(*args, **kwargs):
        raise AssertionError("weekend committee rerun should not use HTTP")

    fake_backend = types.ModuleType("backend.server")
    fake_backend.CommitteeRequest = _FakeRequest
    fake_backend.Holding = _FakeHolding
    fake_backend.run_committee_direct = _fake_direct
    monkeypatch.setitem(sys.modules, "backend.server", fake_backend)
    monkeypatch.setattr(mod.requests, "post", _fail_http)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    config = {
        "total_assets": 100000,
        "cash": 10000,
        "holdings": [
            {
                "symbol": "600900",
                "name": "Changjiang Power",
                "market": "a",
                "position_pct": 5.0,
                "cost": 10.0,
                "sector": "Power",
                "industry": "Utility",
            }
        ],
    }
    summary = {
        "key_themes": ["Power reform"],
        "overall_sentiment": "positive",
        "summary_one_liner": "Power utilities benefit.",
        "stock_impact": {"600900": {"impact": 1, "reason": "benefit"}},
    }

    out = mod.run_committee_on_leaders(["600900"], config, summary)

    assert out[0]["success"] is True
    assert out[0]["verdict"] == "ACCUMULATE"
    assert out[0]["news_impact"] == 1
    assert captured["req"].symbol == "600900"
    assert captured["req"].news_brief


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
