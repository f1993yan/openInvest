"""Deterministic risk-impact model for domestic news.

The model is intentionally simple and auditable. It is an event-study prior:

    impact_bps = - base_bps(category) * severity * reliability * proximity

It reports a 90% interval from model uncertainty rather than pretending to know
the exact market move. The output is meant to make dangerous news actionable in
A-share monitoring without using LLM judgment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence

from services.news_sources import RawNewsItem


@dataclass(frozen=True)
class RiskCategory:
    name: str
    base_bps: float
    keywords: Sequence[tuple[str, float]]
    affected_sectors: Dict[str, float]
    rationale: str


SOURCE_RELIABILITY = {
    "eastmoney": 0.78,
    "sina": 0.74,
    "wallstreetcn": 0.78,
    "baidu_hot": 0.58,
    "cls": 0.82,
    "xueqiu": 0.46,
}


RISK_CATEGORIES: Sequence[RiskCategory] = (
    RiskCategory(
        name="地缘冲突/战争",
        base_bps=85,
        keywords=(
            ("战争", 0.42), ("开战", 0.48), ("导弹", 0.34), ("空袭", 0.40),
            ("军事冲突", 0.44), ("袭击", 0.28), ("伊朗", 0.24), ("以色列", 0.24),
            ("台海", 0.42), ("俄乌", 0.34), ("边境冲突", 0.40),
        ),
        affected_sectors={"军工高端制造": 0.35, "半导体": -0.25, "AI算力": -0.18, "消费": -0.28},
        rationale="地缘冲突通常抬升风险溢价，压制权益估值；军工相对防御。",
    ),
    RiskCategory(
        name="制裁/出口管制",
        base_bps=70,
        keywords=(
            ("制裁", 0.45), ("出口管制", 0.50), ("禁令", 0.35), ("实体清单", 0.50),
            ("加征关税", 0.38), ("贸易战", 0.42), ("断供", 0.44),
        ),
        affected_sectors={"半导体": -0.55, "AI算力": -0.45, "互联网平台": -0.22, "军工高端制造": 0.12},
        rationale="外部约束直接冲击高端制造供应链和成长股风险偏好。",
    ),
    RiskCategory(
        name="金融信用风险",
        base_bps=95,
        keywords=(
            ("债务违约", 0.55), ("违约", 0.38), ("流动性危机", 0.58),
            ("银行挤兑", 0.60), ("爆雷", 0.42), ("破产", 0.35),
            ("系统性风险", 0.62), ("信用风险", 0.42), ("兑付困难", 0.48),
        ),
        affected_sectors={"消费": -0.35, "互联网平台": -0.30, "电力设备": -0.24, "水电公用事业": -0.10},
        rationale="信用冲击会扩大贴现率和风险溢价，优先压制高 beta 资产。",
    ),
    RiskCategory(
        name="公共卫生/疫情",
        base_bps=75,
        keywords=(
            ("疫情", 0.42), ("传染病", 0.38), ("封控", 0.55), ("感染", 0.24),
            ("变异株", 0.35), ("大流行", 0.58), ("疾控", 0.20),
        ),
        affected_sectors={"消费": -0.48, "医药": 0.30, "互联网平台": -0.18},
        rationale="公共卫生冲击影响线下消费和供应链，医药相对受益。",
    ),
    RiskCategory(
        name="重大自然灾害/安全事故",
        base_bps=55,
        keywords=(
            ("地震", 0.46), ("洪水", 0.34), ("台风", 0.28), ("灾难状态", 0.50),
            ("重大事故", 0.46), ("爆炸", 0.36), ("矿难", 0.36), ("停产", 0.28),
        ),
        affected_sectors={"消费": -0.20, "电力设备": -0.12, "水电公用事业": -0.10, "医药": 0.10},
        rationale="灾害和事故会造成局部供给/需求中断，对全市场冲击通常低于金融和战争。",
    ),
    RiskCategory(
        name="强监管/反垄断",
        base_bps=60,
        keywords=(
            ("强监管", 0.42), ("反垄断", 0.48), ("立案调查", 0.35),
            ("处罚", 0.25), ("叫停", 0.36), ("整改", 0.24), ("严查", 0.22),
        ),
        affected_sectors={"互联网平台": -0.55, "消费": -0.15, "新能源车": -0.10},
        rationale="监管冲击更偏结构性，主要影响被监管行业估值。",
    ),
    RiskCategory(
        name="能源/大宗价格冲击",
        base_bps=50,
        keywords=(
            ("油价暴涨", 0.46), ("能源危机", 0.50), ("天然气", 0.22),
            ("煤价", 0.20), ("供电紧张", 0.34), ("限电", 0.42),
        ),
        affected_sectors={"电力设备": 0.12, "水电公用事业": 0.15, "消费": -0.18, "新能源车": -0.12},
        rationale="能源冲击通过成本端压制利润，但电力/能源替代链条可能相对受益。",
    ),
)


def enrich_risk_impacts(items: Iterable[RawNewsItem]) -> List[RawNewsItem]:
    out: List[RawNewsItem] = []
    for item in items:
        impacts = assess_news_risk(item)
        if not impacts:
            out.append(item)
            continue
        meta = dict(item.raw_meta or {})
        meta["risk_impacts"] = impacts
        out.append(RawNewsItem(
            src_name=item.src_name,
            title=item.title,
            url=item.url,
            snippet=item.snippet,
            text=item.text,
            published_at=item.published_at,
            fetched_at=item.fetched_at,
            raw_meta=meta,
        ))
    return out


def assess_news_risk(item: RawNewsItem, *, min_severity: float = 0.62) -> List[Dict[str, Any]]:
    text = f"{item.title}\n{item.snippet}\n{item.text}"
    if not text.strip():
        return []
    reliability = _source_reliability(item.src_name)
    proximity = _a_share_proximity(text)
    impacts: List[Dict[str, Any]] = []
    for category in RISK_CATEGORIES:
        severity, evidence = _category_severity(text, category)
        if severity < min_severity:
            continue
        index_mean = -category.base_bps * severity * reliability * proximity
        sigma = max(12.0, abs(index_mean) * (0.36 + 0.22 * (1 - reliability) + 0.18 * (1 - severity)))
        ci90 = (index_mean - 1.645 * sigma, index_mean + 1.645 * sigma)
        sectors = []
        for sector, beta in category.affected_sectors.items():
            # beta is a relative cushion/benefit factor. Positive beta dampens
            # a negative index shock; negative beta amplifies it.
            sector_mean = index_mean * (1 - beta)
            sectors.append({
                "sector": sector,
                "impact_bps": round(sector_mean, 1),
                "impact_pct": round(sector_mean / 100, 3),
                "relative_cushion": beta,
            })
        sectors.sort(key=lambda row: abs(row["impact_bps"]), reverse=True)
        impacts.append({
            "category": category.name,
            "severity": round(severity, 3),
            "source_reliability": round(reliability, 3),
            "a_share_proximity": round(proximity, 3),
            "csi300_impact_bps": round(index_mean, 1),
            "csi300_impact_pct": round(index_mean / 100, 3),
            "ci90_bps": [round(ci90[0], 1), round(ci90[1], 1)],
            "sector_impacts": sectors,
            "evidence_keywords": evidence,
            "model": {
                "name": "event_study_prior_capm_v1",
                "formula": "-base_bps(category) * severity * source_reliability * a_share_proximity",
                "base_bps": category.base_bps,
                "rationale": category.rationale,
            },
        })
    impacts.sort(key=lambda row: abs(row["csi300_impact_bps"]), reverse=True)
    return impacts


def summarize_risk_impacts(items: Iterable[RawNewsItem], *, max_rows: int = 6) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in items:
        for impact in (item.raw_meta or {}).get("risk_impacts", []):
            rows.append({
                "title": item.title,
                "src_name": item.src_name,
                **impact,
            })
    rows.sort(key=lambda row: abs(row["csi300_impact_bps"]), reverse=True)
    return rows[:max_rows]


def format_risk_impact_brief(items: Iterable[RawNewsItem], *, max_rows: int = 4) -> str:
    rows = summarize_risk_impacts(items, max_rows=max_rows)
    if not rows:
        return ""
    lines = ["重大风险量化影响:"]
    for row in rows:
        ci = row["ci90_bps"]
        top_sector = row["sector_impacts"][0] if row.get("sector_impacts") else {}
        sector_text = ""
        if top_sector:
            sector_text = (
                f"；最大板块冲击 {top_sector.get('sector')}: "
                f"{top_sector.get('impact_bps'):+.1f}bp"
            )
        lines.append(
            f"- [{row['src_name']}] {row['category']} | CSI300 {row['csi300_impact_bps']:+.1f}bp "
            f"(90%区间 {ci[0]:+.1f}~{ci[1]:+.1f}bp){sector_text} | "
            f"证据: {', '.join(row.get('evidence_keywords', [])[:5])}"
        )
    return "\n".join(lines)


def _category_severity(text: str, category: RiskCategory) -> tuple[float, List[str]]:
    normalized = text.lower()
    score = 0.0
    evidence: List[str] = []
    for keyword, weight in category.keywords:
        if keyword.lower() in normalized:
            score += weight
            evidence.append(keyword)
    # Multiple independent danger words should saturate quickly but not exceed 1.
    severity = min(1.0, score)
    if _contains_numbered_damage(text):
        severity = min(1.0, severity + 0.12)
        evidence.append("量化损失/伤亡")
    return severity, evidence[:8]


def _source_reliability(src_name: str) -> float:
    src = (src_name or "").split(":", 1)[0].lower()
    return SOURCE_RELIABILITY.get(src, 0.62)


def _a_share_proximity(text: str) -> float:
    cn_terms = ("中国", "国内", "A股", "沪深", "人民币", "证监会", "央行", "商务部", "工信部", "北京", "上海", "深圳")
    global_terms = ("美国", "欧洲", "中东", "伊朗", "以色列", "俄罗斯", "乌克兰", "全球")
    cn_hit = any(term.lower() in text.lower() for term in cn_terms)
    global_hit = any(term.lower() in text.lower() for term in global_terms)
    if cn_hit:
        return 1.0
    if global_hit:
        return 0.72
    return 0.55


_DAMAGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(人|亿元|亿美元|万亿|%|bp|基点)")


def _contains_numbered_damage(text: str) -> bool:
    return bool(_DAMAGE_RE.search(text or ""))


__all__ = [
    "assess_news_risk",
    "enrich_risk_impacts",
    "summarize_risk_impacts",
    "format_risk_impact_brief",
]
