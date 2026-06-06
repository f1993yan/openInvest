"""Domestic hot-news enrichment.

This module keeps the fetch stage broad: it can ingest general Chinese hot news,
derive investable themes from titles/snippets, and attach likely A/H-share
leaders without requiring an LLM call.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

from services.news_sources import RawNewsItem

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


@dataclass(frozen=True)
class SectorTheme:
    name: str
    keywords: Sequence[str]
    concept_names: Sequence[str]
    leaders: Sequence[Dict[str, str]]


SECTOR_THEMES: Sequence[SectorTheme] = (
    SectorTheme(
        name="AI算力",
        keywords=("AI", "人工智能", "大模型", "算力", "数据中心", "服务器", "GPU", "英伟达", "光模块", "CPO"),
        concept_names=("人工智能", "算力租赁", "数据中心", "CPO概念"),
        leaders=(
            {"symbol": "601138", "name": "工业富联", "market": "A股", "reason": "AI服务器龙头"},
            {"symbol": "300476", "name": "胜宏科技", "market": "A股", "reason": "AI服务器PCB龙头"},
            {"symbol": "000063", "name": "中兴通讯", "market": "A股", "reason": "通信设备与算力基础设施龙头"},
        ),
    ),
    SectorTheme(
        name="半导体",
        keywords=("芯片", "半导体", "集成电路", "晶圆", "封测", "存储", "光刻", "先进封装"),
        concept_names=("半导体", "芯片概念", "先进封装", "存储芯片"),
        leaders=(
            {"symbol": "688981", "name": "中芯国际", "market": "A股", "reason": "晶圆制造龙头"},
            {"symbol": "002185", "name": "华天科技", "market": "A股", "reason": "封装测试龙头"},
            {"symbol": "688099", "name": "晶晨股份", "market": "A股", "reason": "SoC芯片龙头"},
        ),
    ),
    SectorTheme(
        name="机器人",
        keywords=("机器人", "人形机器人", "减速器", "伺服", "具身智能", "自动化"),
        concept_names=("机器人概念", "减速器", "工业机器人"),
        leaders=(
            {"symbol": "688017", "name": "绿的谐波", "market": "A股", "reason": "谐波减速器龙头"},
            {"symbol": "300124", "name": "汇川技术", "market": "A股", "reason": "工业自动化龙头"},
        ),
    ),
    SectorTheme(
        name="新能源车",
        keywords=("新能源车", "电动车", "汽车", "电池", "锂电", "固态电池", "充电桩", "智能驾驶"),
        concept_names=("新能源汽车", "锂电池", "固态电池", "智能驾驶"),
        leaders=(
            {"symbol": "300750", "name": "宁德时代", "market": "A股", "reason": "动力电池龙头"},
            {"symbol": "002594", "name": "比亚迪", "market": "A股", "reason": "新能源车龙头"},
        ),
    ),
    SectorTheme(
        name="电力设备",
        keywords=("电网", "特高压", "输变电", "电力设备", "变压器", "储能", "新能源消纳"),
        concept_names=("特高压", "智能电网", "储能", "电力设备"),
        leaders=(
            {"symbol": "601179", "name": "中国西电", "market": "A股", "reason": "输变电设备龙头"},
            {"symbol": "600580", "name": "卧龙电驱", "market": "A股", "reason": "工业电机龙头"},
        ),
    ),
    SectorTheme(
        name="水电公用事业",
        keywords=("用电负荷", "电力保供", "水电", "火电", "公用事业", "电价", "迎峰度夏"),
        concept_names=("公用事业", "水电", "电力行业"),
        leaders=(
            {"symbol": "600900", "name": "长江电力", "market": "A股", "reason": "水电运营龙头"},
            {"symbol": "600886", "name": "国投电力", "market": "A股", "reason": "综合电力运营龙头"},
        ),
    ),
    SectorTheme(
        name="消费",
        keywords=("消费", "白酒", "食品", "旅游", "餐饮", "免税", "暑运", "电影", "家电"),
        concept_names=("白酒", "食品饮料", "旅游酒店", "家用电器"),
        leaders=(
            {"symbol": "600519", "name": "贵州茅台", "market": "A股", "reason": "白酒龙头"},
            {"symbol": "000333", "name": "美的集团", "market": "A股", "reason": "家电龙头"},
        ),
    ),
    SectorTheme(
        name="医药",
        keywords=("医药", "创新药", "医疗", "疫苗", "医保", "CXO", "药品", "医院"),
        concept_names=("创新药", "医疗器械", "CXO概念", "生物医药"),
        leaders=(
            {"symbol": "600276", "name": "恒瑞医药", "market": "A股", "reason": "创新药龙头"},
            {"symbol": "300760", "name": "迈瑞医疗", "market": "A股", "reason": "医疗器械龙头"},
        ),
    ),
    SectorTheme(
        name="互联网平台",
        keywords=("互联网", "平台经济", "游戏", "电商", "云计算", "腾讯", "阿里", "字节", "短剧"),
        concept_names=("互联网电商", "网络游戏", "云计算", "平台经济"),
        leaders=(
            {"symbol": "00700", "name": "腾讯控股", "market": "港股", "reason": "互联网平台龙头"},
            {"symbol": "09988", "name": "阿里巴巴", "market": "港股", "reason": "电商与云计算龙头"},
        ),
    ),
    SectorTheme(
        name="军工高端制造",
        keywords=("军工", "航空", "航天", "低空经济", "大飞机", "无人机", "高端装备"),
        concept_names=("军工", "低空经济", "大飞机", "航空航天"),
        leaders=(
            {"symbol": "603308", "name": "应流股份", "market": "A股", "reason": "高端铸件与航空航天供应链"},
            {"symbol": "600760", "name": "中航沈飞", "market": "A股", "reason": "航空主机厂龙头"},
        ),
    ),
)


def fetch_baidu_hot_news(*, max_items: int = 20) -> List[RawNewsItem]:
    """Fetch broad Chinese hot topics from Baidu Top API."""
    now = _now()
    try:
        resp = requests.get(
            "https://top.baidu.com/api/board",
            params={"tab": "realtime"},
            headers={"User-Agent": _UA, "Referer": "https://top.baidu.com/"},
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
        cards = data.get("data", {}).get("cards", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("baidu_hot fetch failed: %s", exc)
        return []

    items: List[RawNewsItem] = []
    for entry in _iter_card_entries(cards):
        title = str(entry.get("word") or entry.get("query") or entry.get("title") or "").strip()
        if not title:
            continue
        desc = str(entry.get("desc") or entry.get("hotDesc") or "")
        url = str(entry.get("url") or entry.get("appUrl") or "")
        if not url:
            url = f"https://www.baidu.com/s?wd={title}"
        hot_score = entry.get("hotScore") or entry.get("hotScoreNum") or entry.get("index")
        items.append(
            RawNewsItem(
                src_name="baidu_hot",
                title=title,
                url=url,
                snippet=desc[:300],
                fetched_at=now,
                raw_meta={"hot_score": hot_score, "source": "baidu_top"},
            )
        )
        if len(items) >= max_items:
            break
    return enrich_news_items(items)


def enrich_news_items(items: Iterable[RawNewsItem], *, max_leaders: int = 3) -> List[RawNewsItem]:
    """Attach sector and leader metadata to news items in-place-compatible form."""
    out: List[RawNewsItem] = []
    for item in items:
        text = f"{item.title}\n{item.snippet}\n{item.text}"
        sectors = infer_sectors(text)
        if not sectors:
            out.append(item)
            continue
        sector_payload = []
        for sector in sectors:
            leaders = find_sector_leaders(sector, max_leaders=max_leaders)
            sector_payload.append(
                {
                    "sector": sector.name,
                    "keywords": [kw for kw in sector.keywords if kw.lower() in text.lower()][:5],
                    "leaders": leaders,
                    "concept_names": list(sector.concept_names),
                }
            )
        meta = dict(item.raw_meta or {})
        meta["sectors"] = sector_payload
        out.append(
            RawNewsItem(
                src_name=item.src_name,
                title=item.title,
                url=item.url,
                snippet=item.snippet,
                text=item.text,
                published_at=item.published_at,
                fetched_at=item.fetched_at,
                raw_meta=meta,
            )
        )
    try:
        from services.news_sources.news_risk_model import enrich_risk_impacts
        return enrich_risk_impacts(out)
    except Exception as exc:
        log.warning("news risk impact enrichment failed: %s", exc)
        return out


def infer_sectors(text: str, *, limit: int = 3) -> List[SectorTheme]:
    normalized = text.lower()
    scored: List[tuple[int, SectorTheme]] = []
    for theme in SECTOR_THEMES:
        score = 0
        for kw in theme.keywords:
            if kw.lower() in normalized:
                score += 2 if len(kw) >= 4 else 1
        if score:
            scored.append((score, theme))
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return [theme for _, theme in scored[:limit]]


def find_sector_leaders(theme: SectorTheme, *, max_leaders: int = 3) -> List[Dict[str, Any]]:
    """Use akshare concept constituents when available, otherwise fallback."""
    if os.getenv("INVEST_NEWS_AKSHARE_LEADERS", "0") == "1":
        ak_leaders = _find_akshare_leaders(theme, max_leaders=max_leaders)
        if ak_leaders:
            return ak_leaders
    return [dict(item, source="fallback_theme_map") for item in theme.leaders[:max_leaders]]


def format_sector_brief(items: Iterable[RawNewsItem], *, max_items: int = 8) -> str:
    lines: List[str] = []
    try:
        from services.news_sources.news_risk_model import format_risk_impact_brief
        risk_brief = format_risk_impact_brief(items)
        if risk_brief:
            lines.extend([risk_brief, ""])
    except Exception:
        pass
    for item in list(items)[:max_items]:
        title = (item.title or "").strip()[:100]
        if not title:
            continue
        lines.append(f"- [{item.src_name}] {title}")
        for sector in (item.raw_meta or {}).get("sectors", [])[:2]:
            leader_text = "、".join(
                f"{leader.get('name')}({leader.get('symbol')})"
                for leader in sector.get("leaders", [])[:3]
                if leader.get("name") and leader.get("symbol")
            )
            if leader_text:
                lines.append(f"  涉及板块: {sector.get('sector')}；对应龙头: {leader_text}")
            else:
                lines.append(f"  涉及板块: {sector.get('sector')}")
        for impact in (item.raw_meta or {}).get("risk_impacts", [])[:1]:
            ci = impact.get("ci90_bps", ["?", "?"])
            lines.append(
                f"  危险信号: {impact.get('category')}；"
                f"CSI300模型冲击 {impact.get('csi300_impact_bps'):+.1f}bp "
                f"(90%区间 {ci[0]:+.1f}~{ci[1]:+.1f}bp)"
            )
    return "\n".join(lines)


def summarize_sector_hits(items: Iterable[RawNewsItem], *, max_sectors: int = 8) -> List[Dict[str, Any]]:
    summary: Dict[str, Dict[str, Any]] = {}
    for item in items:
        for sector in (item.raw_meta or {}).get("sectors", []):
            name = sector.get("sector")
            if not name:
                continue
            bucket = summary.setdefault(name, {"sector": name, "count": 0, "leaders": [], "sources": set()})
            bucket["count"] += 1
            bucket["sources"].add(item.src_name)
            seen_symbols = {leader.get("symbol") for leader in bucket["leaders"]}
            for leader in sector.get("leaders", []):
                if leader.get("symbol") not in seen_symbols:
                    bucket["leaders"].append(leader)
                    seen_symbols.add(leader.get("symbol"))
    rows = list(summary.values())
    rows.sort(key=lambda row: (-row["count"], row["sector"]))
    for row in rows:
        row["sources"] = sorted(row["sources"])
        row["leaders"] = row["leaders"][:3]
    return rows[:max_sectors]


def _iter_card_entries(cards: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for card in cards:
        content = card.get("content")
        if isinstance(content, list):
            for entry in content:
                if isinstance(entry, dict) and isinstance(entry.get("content"), list):
                    yield from _iter_card_entries([entry])
                elif isinstance(entry, dict):
                    yield entry
        elif isinstance(card, dict):
            yield card


def _find_akshare_leaders(theme: SectorTheme, *, max_leaders: int) -> List[Dict[str, Any]]:
    try:
        import akshare as ak  # type: ignore
    except Exception:
        return []

    for concept in theme.concept_names:
        try:
            df = ak.stock_board_concept_cons_em(symbol=concept)
        except Exception:
            continue
        rows = _rank_stock_rows(df, max_leaders=max_leaders)
        if rows:
            return [
                {
                    "symbol": row["symbol"],
                    "name": row["name"],
                    "market": "A股",
                    "reason": f"{concept}成分股综合排序靠前",
                    "source": "akshare_concept",
                    "score": row["score"],
                }
                for row in rows
            ]
    return []


def _rank_stock_rows(df: Any, *, max_leaders: int) -> List[Dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []

    def col(*names: str) -> Optional[str]:
        for name in names:
            if name in df.columns:
                return name
        return None

    code_col = col("代码", "股票代码", "symbol")
    name_col = col("名称", "股票简称", "name")
    if not code_col or not name_col:
        return []

    change_col = col("涨跌幅", "涨跌幅(%)")
    amount_col = col("成交额")
    value_col = col("总市值", "流通市值")

    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        symbol = _clean_symbol(row.get(code_col))
        name = str(row.get(name_col) or "").strip()
        if not symbol or not name:
            continue
        score = 0.0
        score += _safe_float(row.get(change_col)) * 2 if change_col else 0.0
        score += _scaled(_safe_float(row.get(amount_col))) * 1.5 if amount_col else 0.0
        score += _scaled(_safe_float(row.get(value_col))) if value_col else 0.0
        rows.append({"symbol": symbol, "name": name, "score": round(score, 4)})
    rows.sort(key=lambda row: (-row["score"], row["symbol"]))
    return rows[:max_leaders]


def _clean_symbol(value: Any) -> str:
    match = re.search(r"\d{5,6}", str(value or ""))
    return match.group(0) if match else ""


def _safe_float(value: Any) -> float:
    try:
        text = str(value).replace(",", "").replace("%", "").strip()
        if text in ("", "-", "nan", "None"):
            return 0.0
        return float(text)
    except Exception:
        return 0.0


def _scaled(value: float) -> float:
    if value <= 0:
        return 0.0
    return min(value / 1_000_000_000, 100.0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "SECTOR_THEMES",
    "fetch_baidu_hot_news",
    "enrich_news_items",
    "infer_sectors",
    "find_sector_leaders",
    "format_sector_brief",
    "summarize_sector_hits",
]
