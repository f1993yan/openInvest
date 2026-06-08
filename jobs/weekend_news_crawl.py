"""周末新闻抓取 + 缓存 + 周日晚间总结 + 委员会评估

定时执行（周六、周日 12:00 和 20:00）：
  1. 抓取所有国内新闻源（东方财富/财联社/新浪/雪球/华尔街见闻/百度热搜）
  2. 保存到 data/weekend_news/<日期>_<时间>.json 缓存
  3. 周日 20:00 额外：
     a. 合并周末两天所有缓存新闻
     b. LLM 总结关键主题、涉及 A 股板块和候选龙头股
     c. 输出可跟踪的热门股票机会池

缓存结构:
  data/weekend_news/
    ├── 2026-06-06_Sat_1200.json
    ├── 2026-06-06_Sat_2000.json
    ├── 2026-06-07_Sun_1200.json
    ├── 2026-06-07_Sun_2000.json
    └── summary_2026-06-07.json
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

CACHE_DIR = _PROJECT_ROOT / "data" / "weekend_news"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = _PROJECT_ROOT / "jobs" / "market_monitor_config.json"
BACKEND_URL = os.getenv("INVEST_BACKEND_URL", "http://127.0.0.1:8766")
# 历史兼容：持仓 ≥ 此百分比的才进入旧的委员会重评流程。
# 新的周末新闻总结不再默认评估持仓，而是从新闻中寻找 A 股热门题材和候选龙头。
LEADING_PCT_THRESHOLD = float(os.getenv("INVEST_WEEKEND_LEADING_PCT", "5.0"))

log = logging.getLogger("weekend_news")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [weekend_news] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(CACHE_DIR / "crawl.log", encoding="utf-8"),
    ],
)


def _load_local_env() -> None:
    """Load project .env for scheduler/manual runs without exposing values."""
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_path, override=False)
        return
    except Exception:
        pass

    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as exc:  # noqa: BLE001
        log.warning("加载 .env 失败: %s", exc)


# ==========================================
# 工具
# ==========================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_local() -> datetime:
    return datetime.now()


def _hour_slot() -> str:
    """返回当前时间槽标签，如 'Sat_1200'"""
    now = _now_local()
    day_abbr = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][now.weekday()]
    return f"{now.strftime('%Y-%m-%d')}_{day_abbr}_{now.strftime('%H%M')}"


def _load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


# ==========================================
# Step 1: 抓取新闻 + 缓存
# ==========================================

def crawl_and_cache() -> Dict[str, Any]:
    """抓取所有国内新闻源，返回统计信息"""
    from services.news_sources import fetch_all

    log.info("开始抓取国内新闻源...")
    items = fetch_all(domestic=True, max_per_source=30, timeout_sec=60)
    log.info(f"抓取完成: {len(items)} 条（去重后）")

    slot = _hour_slot()
    cache_file = CACHE_DIR / f"{slot}.json"

    payload = {
        "slot": slot,
        "fetched_at": _now_iso(),
        "total_items": len(items),
        "items": [
            {
                "src_name": it.src_name,
                "title": it.title,
                "url": it.url,
                "snippet": it.snippet,
                "text": it.text[:500] if it.text else "",
                "published_at": it.published_at,
                "fetched_at": it.fetched_at,
                "raw_meta": it.raw_meta or {},
            }
            for it in items
        ],
    }

    cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"缓存已写入: {cache_file} ({cache_file.stat().st_size} bytes)")

    # 按源统计
    from collections import Counter
    src_counts = Counter(it.src_name for it in items)
    stats = {
        "slot": slot,
        "total": len(items),
        "by_source": dict(src_counts.most_common()),
        "cache_file": str(cache_file),
    }
    return stats


# ==========================================
# Step 2: 加载周末缓存
# ==========================================

def load_weekend_caches() -> List[Dict[str, Any]]:
    """加载本周六/日的所有缓存文件（按时间排序，只看已过去的日期）"""
    now = _now_local()
    # weekday: Mon=0, Sun=6, Sat=5
    days_since_monday = now.weekday()

    # 找到最近的周六和周日（一定是 ≤ today）
    # 如果今天是周五或更早 → 上周六/日
    # 如果今天是周六 → 周六=today, 周日=昨天（还没到）
    # 如果今天是周日 → 周六=yesterday, 周日=today
    if days_since_monday < 5:
        # Mon-Thu: 上周六/日
        sat_date = (now + timedelta(days=5 - days_since_monday - 7)).strftime("%Y-%m-%d")
        sun_date = (now + timedelta(days=6 - days_since_monday - 7)).strftime("%Y-%m-%d")
    elif days_since_monday == 5:
        # Saturday: only today
        sat_date = now.strftime("%Y-%m-%d")
        sun_date = ""  # 周日还没到
    else:
        # Sunday: yesterday (Sat) + today (Sun)
        sat_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        sun_date = now.strftime("%Y-%m-%d")

    log.info(f"查找周末缓存: {sat_date} ~ {sun_date}")

    caches: List[Dict[str, Any]] = []
    target_dates = [d for d in (sat_date, sun_date) if d]  # 过滤空串
    for json_file in sorted(CACHE_DIR.glob("*.json")):
        name = json_file.stem  # e.g. "2026-06-06_Sat_1200"
        # 只加载目标日期的文件
        if not any(date_str in name for date_str in target_dates):
            continue
        # 跳过 summary 和 report 文件
        if name.startswith("summary_") or name.startswith("report_"):
            continue
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            caches.append(data)
            log.info(f"  加载: {json_file.name} ({data.get('total_items', 0)} 条)")
        except Exception as e:
            log.warning(f"  跳过损坏缓存 {json_file.name}: {e}")

    return caches


# ==========================================
# Step 3: LLM 总结 + A 股热门题材/龙头发现
# ==========================================

_SUMMARIZE_PROMPT = """你是一个资深 A 股主题投资分析师。下面是从周六到周日从多个国内新闻源
（东方财富、财联社、新浪财经、雪球、华尔街见闻、百度热搜）抓取的所有新闻标题和摘要。

你的任务不是评估用户已有持仓，而是从这些新闻里寻找“下一个可能被市场炒作的 A 股热门方向”。
请把新闻事件提炼成可投资主题，映射到 A 股板块/概念，并给出候选龙头股票。

请完成以下分析：

## 1. 周末关键主题（3-5个）
用一两句中文概括本周末最重要的宏观/政策/产业/科技/消费主题。

## 2. A 股板块与概念映射
对每个主题，列出最可能受资金关注的 A 股板块/概念，例如 AI算力、机器人、商业航天、脑机接口、低空经济、半导体、创新药、电力设备等。
必须说明“新闻证据 -> 板块逻辑”，不要只给结论。
最多输出 5 个 sector_opportunities。

## 3. 候选龙头股
为每个板块给出 2-3 只 A 股候选龙头或弹性标的，优先选择：
- A 股上市公司，股票代码为 6 位数字；
- 与新闻事件的业务相关性明确；
- 市场通常认可的板块龙头、细分龙头或高弹性核心标的。

每只股票需要给出：
- symbol: 6 位 A 股代码；
- name: 中文简称；
- reason: 为什么它对应这个新闻主题；
- leader_type: "行业龙头" / "细分龙头" / "高弹性标的" / "事件受益标的"；
- confidence: 0-1；
- evidence_titles: 支撑该映射的新闻标题，最多 3 条；
- risk_note: 追高、兑现、政策不确定性、海外映射不足等风险。

## 4. 热门股票机会排序
从所有候选股中选出 5-8 个最值得后续跟踪的标的，按“新闻热度、题材新鲜度、A 股映射清晰度、龙头确定性”排序。

重要约束：
- 不要围绕用户持仓分析。
- 不要输出港股或美股作为候选龙头；如果新闻源是海外公司或港股公司，只能寻找其 A 股映射链条。
- 如果新闻无法映射到 A 股，请明确写入 rejected_topics。
- 不要声称这是买入建议，只能说“候选跟踪标的”。
- 输出必须简洁，reason 和 risk_note 都控制在 40 个中文字以内。

---

## 周末新闻汇总

{news_text}

---

请用以下 JSON 格式输出（不要包含其他内容）：

```json
{{
  "key_themes": ["主题1", "主题2", "..."],
  "theme_detail": {{"主题1": "一句话说明", "...": "..."}},
  "sector_opportunities": [
    {{
      "theme": "主题名",
      "sector": "A股板块或概念",
      "logic": "新闻证据到板块的推理链",
      "heat_score": 0.0,
      "freshness_score": 0.0,
      "evidence_titles": ["新闻标题1", "新闻标题2"],
      "leaders": [
        {{
          "symbol": "000000",
          "name": "公司简称",
          "leader_type": "行业龙头",
          "reason": "为什么是该主题候选龙头",
          "confidence": 0.0,
          "risk_note": "主要风险",
          "evidence_titles": ["新闻标题1"]
        }}
      ]
    }}
  ],
  "hot_stock_opportunities": [
    {{
      "rank": 1,
      "symbol": "000000",
      "name": "公司简称",
      "sector": "A股板块或概念",
      "theme": "主题名",
      "score": 0.0,
      "reason": "进入机会池的核心理由",
      "risk_note": "主要风险"
    }}
  ],
  "watchlist_symbols": ["000000", "000001"],
  "rejected_topics": [
    {{"theme": "主题名", "reason": "无法映射到A股或证据不足"}}
  ],
  "overall_sentiment": "positive/neutral/negative",
  "summary_one_liner": "一句话总结本周末最重要的变化"
}}
```
"""


def _build_news_text(caches: List[Dict[str, Any]]) -> str:
    """合并所有缓存新闻为纯文本"""
    seen_titles = set()
    lines: List[str] = []

    for cache in caches:
        slot = cache.get("slot", "?")
        lines.append(f"\n### {slot} ({cache.get('total_items', 0)} 条)")
        for item in cache.get("items", []):
            title = (item.get("title") or "").strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            src = item.get("src_name", "?")
            snippet = (item.get("snippet") or item.get("text") or "")[:200]
            lines.append(f"- [{src}] {title}")
            if snippet and snippet != title:
                lines.append(f"  {snippet}")

    return "\n".join(lines)


def _build_leading_stocks_text(config: Dict[str, Any]) -> str:
    """构建龙头股列表文本"""
    holdings = config.get("holdings", [])
    leading = [h for h in holdings if h.get("position_pct", 0) >= LEADING_PCT_THRESHOLD]
    leading.sort(key=lambda h: h.get("position_pct", 0), reverse=True)

    lines = []
    for h in leading:
        lines.append(
            f"- {h['symbol']} {h['name']}: "
            f"仓位 {h['position_pct']:.1f}%, "
            f"行业 {h.get('sector', '')}/{h.get('industry', '')}"
        )
    return "\n".join(lines)


def _empty_summary(message: str) -> Dict[str, Any]:
    return {
        "error": message,
        "key_themes": [],
        "theme_detail": {},
        "sector_opportunities": [],
        "hot_stock_opportunities": [],
        "watchlist_symbols": [],
        "rejected_topics": [],
        "need_committee_rerun": [],
        "overall_sentiment": "neutral",
        "summary_one_liner": message,
    }


def summarize_and_evaluate(caches: List[Dict[str, Any]],
                           config: Dict[str, Any]) -> Dict[str, Any]:
    """LLM 总结周末新闻，并发现 A 股板块/龙头机会。

    config is retained for backward compatibility with callers, but this
    analysis intentionally does not use portfolio holdings as the candidate
    universe. The goal is to discover new hot A-share ideas from news.
    """
    if not caches:
        return _empty_summary("无周末缓存数据")

    total_items = sum(c.get("total_items", 0) for c in caches)
    log.info(f"开始 LLM 新闻机会发现: {len(caches)} 个缓存, 共 {total_items} 条新闻")

    news_text = _build_news_text(caches)

    prompt = _SUMMARIZE_PROMPT.format(
        news_text=news_text[:12000],  # 截断防 token 超限
    )

    # 调 LLM
    from openai import OpenAI
    from utils.llm import get_llm_config_safe

    _load_local_env()
    api_key, base_url, model, _ = get_llm_config_safe()
    if not api_key:
        return _empty_summary("LLM API key 未设，无法总结")

    client = OpenAI(api_key=api_key, base_url=base_url)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一个专业的中文宏观分析师。请严格按 JSON 格式输出，不要输出任何多余内容。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=8000,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
    except Exception as e:
        log.error(f"LLM 调用失败: {e}")
        return _empty_summary(f"LLM 调用失败: {e}")

    # 解析 JSON
    try:
        # 提取 JSON 块
        import re
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', content)
        if match:
            content = match.group(1).strip()
        result = json.loads(content)
    except json.JSONDecodeError:
        log.warning(f"LLM 返回非 JSON，尝试兼容解析: {content[:500]}")
        # 兼容：尝试从文本中提取关键信息
        result = {
            "key_themes": [],
            "theme_detail": {},
            "sector_opportunities": [],
            "hot_stock_opportunities": [],
            "watchlist_symbols": [],
            "rejected_topics": [],
            "need_committee_rerun": [],
            "overall_sentiment": "neutral",
            "summary_one_liner": content[:200],
        }

    result = _normalize_opportunity_summary(result)
    result["_total_news"] = total_items
    result["_cache_count"] = len(caches)
    result["_raw_response"] = content[:500]

    # 保存 summary
    summary_file = CACHE_DIR / f"summary_{_now_local().strftime('%Y-%m-%d')}.json"
    summary_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"总结已保存: {summary_file}")

    return result


def _normalize_opportunity_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize LLM JSON into a stable opportunity schema."""
    if not isinstance(result, dict):
        result = {}

    result.setdefault("key_themes", [])
    result.setdefault("theme_detail", {})
    result.setdefault("sector_opportunities", [])
    result.setdefault("hot_stock_opportunities", [])
    result.setdefault("watchlist_symbols", [])
    result.setdefault("rejected_topics", [])
    result.setdefault("overall_sentiment", "neutral")
    result.setdefault("summary_one_liner", "")

    # Backward compatibility: this task is no longer about portfolio reruns.
    result["need_committee_rerun"] = []

    valid_watchlist: List[str] = []
    for row in result.get("hot_stock_opportunities", []) or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).strip()
        if _is_a_share_symbol(symbol) and symbol not in valid_watchlist:
            valid_watchlist.append(symbol)
    for symbol in result.get("watchlist_symbols", []) or []:
        symbol = str(symbol).strip()
        if _is_a_share_symbol(symbol) and symbol not in valid_watchlist:
            valid_watchlist.append(symbol)
    result["watchlist_symbols"] = valid_watchlist[:10]

    for idx, row in enumerate(result.get("hot_stock_opportunities", []) or [], start=1):
        if isinstance(row, dict):
            row.setdefault("rank", idx)
            row.setdefault("score", 0.0)
            row.setdefault("risk_note", "")

    return result


def _is_a_share_symbol(symbol: str) -> bool:
    return bool(symbol and len(symbol) == 6 and symbol.isdigit())


def send_weekend_news_popup(report: Dict[str, Any]) -> None:
    """Show a topmost Windows popup with the hot A-share opportunities."""
    import ctypes
    import threading

    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    title = "周末新闻机会池"
    lines: List[str] = []

    one_liner = summary.get("summary_one_liner") or ""
    if one_liner:
        lines.append(str(one_liner)[:120])

    themes = summary.get("key_themes") or []
    if themes:
        lines.append("")
        lines.append("热门主题:")
        for theme in themes[:5]:
            lines.append(f"- {theme}")

    opportunities = summary.get("hot_stock_opportunities") or []
    if opportunities:
        lines.append("")
        lines.append("候选跟踪标的:")
        for row in opportunities[:8]:
            if not isinstance(row, dict):
                continue
            rank = row.get("rank", "")
            symbol = row.get("symbol", "")
            name = row.get("name", "")
            sector = row.get("sector", "")
            reason = str(row.get("reason") or "")[:45]
            prefix = f"{rank}. " if rank else "- "
            lines.append(f"{prefix}{name}({symbol}) [{sector}]")
            if reason:
                lines.append(f"   {reason}")

    sectors = summary.get("sector_opportunities") or []
    if sectors:
        lines.append("")
        lines.append("涉及板块:")
        for row in sectors[:5]:
            if isinstance(row, dict):
                lines.append(f"- {row.get('sector')} / {row.get('theme')}")

    if not lines:
        lines = ["未发现明确 A 股候选机会。"]

    body = "\n".join(lines[:28])

    def _show() -> None:
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                body,
                title,
                0x40 | 0x40000,  # MB_ICONINFORMATION | MB_TOPMOST
            )
        except Exception:
            pass

    threading.Thread(target=_show, daemon=True).start()


# ==========================================
# Step 4: 跑委员会
# ==========================================

def run_committee_on_leaders(symbols: List[str],
                             config: Dict[str, Any],
                             summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """对每个龙头股跑委员会评估（通过后端 API，不依赖 memory/user.md）"""
    if not symbols:
        log.info("无需跑的标的")
        return []

    holdings = config.get("holdings", [])
    total_assets = config.get("total_assets", 100000)
    cash = config.get("cash", 0)

    # 构建 event_brief（从 summary 中提取）
    themes = summary.get("key_themes", [])
    sentiment = summary.get("overall_sentiment", "neutral")
    one_liner = summary.get("summary_one_liner", "")

    event_brief_lines = [
        f"## 📰 周末新闻委员会特别评估",
        f"整体情绪: {sentiment}",
        f"核心主题: {', '.join(themes[:5])}",
        f"总结: {one_liner}",
        "",
        "### 各股影响评估",
    ]
    for sym, impact in summary.get("stock_impact", {}).items():
        imp = impact.get("impact", 0)
        reason = impact.get("reason", "")
        emoji = {1: "🟢", 0: "⚪", -1: "🔴"}.get(imp, "⚪")
        event_brief_lines.append(f"- {emoji} {sym}: {reason}")
    news_brief = "\n".join(event_brief_lines)

    # 逐只通过后端 API 调用委员会
    results: List[Dict[str, Any]] = []
    for sym in symbols:
        stock = next((h for h in holdings if h["symbol"] == sym), None)
        if stock is None:
            log.warning(f"{sym} 不在持仓中，跳过")
            continue

        # 构建 holdings 列表（排除当前标的）
        other_holdings = [
            {
                "symbol": h["symbol"],
                "name": h["name"],
                "weight_pct": h["position_pct"],
                "cost": h["cost"],
            }
            for h in holdings
            if h["symbol"] != sym
        ]

        payload = {
            "symbol": sym,
            "name": stock["name"],
            "market": stock.get("market", "a"),
            "sector": stock.get("sector", ""),
            "industry": stock.get("industry", ""),
            "position_pct": stock.get("position_pct", 0),
            "cost": stock.get("cost", 0),
            "total_assets": total_assets,
            "cash": cash,
            "holdings": other_holdings,
            "min_lot_size": 100 if stock.get("market", "a") == "a" else 100,
            "t_plus_1": stock.get("market", "a") == "a",
            "available_cash": cash,
            "t2_pending_cash": config.get("t2_pending_cash", 0),
            "news_brief": news_brief,
            "fundamentals": stock.get("fundamentals", {}),
            "max_debate_rounds": 3,
        }

        try:
            resp = requests.post(
                f"{BACKEND_URL}/api/committee",
                json=payload,
                timeout=180,
            )
            data = resp.json()
        except Exception as e:
            log.error(f"委员会 API {sym} 失败: {e}")
            data = {"success": False, "symbol": sym, "error": str(e)}

        entry = {
            "symbol": sym,
            "success": data.get("success", False),
            "verdict": data.get("verdict", ""),
            "confidence": data.get("confidence", 0),
            "suggested_alloc_cny": data.get("suggested_alloc_cny", 0),
            "cio_memo": (data.get("cio_memo") or "")[:500],
            "error": data.get("error", ""),
        }
        # 附加新闻影响
        si = summary.get("stock_impact", {}).get(sym, {})
        if si:
            entry["news_impact"] = si.get("impact", 0)
            entry["news_reason"] = si.get("reason", "")
        results.append(entry)
        log.info(f"  {sym}: {entry.get('verdict', 'ERROR')} "
                 f"(conf={entry.get('confidence', 0):.2f}, "
                 f"news_impact={entry.get('news_impact', '?')})")

        # 避免过快连续请求
        import time
        time.sleep(1)

    return results


# ==========================================
# 主入口
# ==========================================

def run() -> Dict[str, Any]:
    """调度器入口"""
    now = _now_local()
    weekday = now.weekday()  # Mon=0, Sun=6
    hour = now.hour

    log.info(f"=== 周末新闻抓取启动: {now.strftime('%Y-%m-%d %H:%M')} (weekday={weekday}) ===")

    # Step 1: 抓取 + 缓存（每次必做）
    cache_stats = crawl_and_cache()

    # Step 2: 判断是否周日晚 8 点（额外做总结 + 委员会）
    is_sunday_evening = (weekday == 6 and hour >= 19)
    # 也允许手动触发 --force-eval
    force_eval = "--force-eval" in sys.argv

    if not is_sunday_evening and not force_eval:
        log.info("非周日晚间，仅抓取缓存。完成。")
        return {
            "status": "cached",
            "slot": cache_stats["slot"],
            "total_news": cache_stats["total"],
            "by_source": cache_stats["by_source"],
            "next_step": "等待周日晚 20:00 进行新闻主题、A股板块与候选龙头总结",
        }

    log.info("周日晚间模式：加载周末缓存 → LLM发现A股板块与候选龙头")

    # Step 3: 加载周末所有缓存
    caches = load_weekend_caches()
    if not caches:
        log.warning("无周末缓存！仅用当前轮次新闻")
        caches = [{
            "slot": cache_stats["slot"],
            "total_items": cache_stats["total"],
            "items": json.loads(
                Path(cache_stats["cache_file"]).read_text(encoding="utf-8")
            ).get("items", []),
        }]

    # Step 4: 加载配置。新闻机会发现不依赖持仓；配置仅供旧委员会重评兼容使用。
    config = _load_config()
    if not config.get("holdings"):
        log.warning("无持仓配置；继续生成新闻机会报告，跳过委员会")

    # Step 5: LLM 总结 + A 股板块/候选龙头发现
    summary = summarize_and_evaluate(caches, config)
    log.info(f"总结完成: sentiment={summary.get('overall_sentiment')}, "
              f"themes={len(summary.get('key_themes', []))}, "
              f"watchlist={summary.get('watchlist_symbols', [])}")

    # Step 6: 历史兼容的委员会评估。新逻辑默认不评估用户持仓。
    rerun_symbols = summary.get("need_committee_rerun", [])
    committee_results: List[Dict[str, Any]] = []
    if rerun_symbols:
        committee_results = run_committee_on_leaders(rerun_symbols, config, summary)

    # Step 7: 生成最终报告
    report = {
        "status": "complete",
        "timestamp": _now_iso(),
        "cache_slot": cache_stats["slot"],
        "total_news_crawled": cache_stats["total"],
        "weekend_caches_loaded": len(caches),
        "total_news_merged": summary.get("_total_news", 0),
        "summary": {
            "key_themes": summary.get("key_themes", []),
            "theme_detail": summary.get("theme_detail", {}),
            "overall_sentiment": summary.get("overall_sentiment", "neutral"),
            "summary_one_liner": summary.get("summary_one_liner", ""),
            "sector_opportunities": summary.get("sector_opportunities", []),
            "hot_stock_opportunities": summary.get("hot_stock_opportunities", []),
            "watchlist_symbols": summary.get("watchlist_symbols", []),
            "rejected_topics": summary.get("rejected_topics", []),
        },
        "committee": {
            "symbols_evaluated": rerun_symbols,
            "results": committee_results,
        },
    }

    # 保存最终报告
    report_file = CACHE_DIR / f"report_{now.strftime('%Y-%m-%d_%H%M')}.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"最终报告: {report_file}")

    if os.getenv("INVEST_WEEKEND_NEWS_POPUP", "1") != "0":
        send_weekend_news_popup(report)

    # 简要日志
    action_count = sum(
        1 for r in committee_results
        if r.get("verdict") not in ("HOLD", "UNCLEAR", "")
    )
    log.info(f"=== 周日晚间评估完成 === "
              f"新闻 {summary.get('_total_news', 0)} 条 → "
              f"主题 {len(summary.get('key_themes', []))} 个 → "
              f"候选标的 {len(summary.get('watchlist_symbols', []))} 只, "
              f"委员会 {len(committee_results)} 只, "
              f"需操作 {action_count} 只")

    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
