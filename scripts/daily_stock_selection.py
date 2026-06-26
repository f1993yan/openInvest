"""Run daily A-share stock selection.

This command builds a deterministic daily watchlist from domestic news and
A-share OHLCV behavior. It writes JSON under data/daily_stock_selection/ by
default; the data directory is intentionally local-only.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

from core.daily_stock_selector import build_daily_selection, result_to_dict
from services.news_sources import RawNewsItem, fetch_all
from services.news_sources.domestic_hot_news import enrich_news_items, extract_stock_symbols_from_text
from utils.akshare_data import get_history_data


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "daily_stock_selection"
MONITOR_CONFIG_PATH = ROOT / "jobs" / "market_monitor_config.json"


def run_daily_stock_selection(
    *,
    trade_date: str | None = None,
    max_news: int = 30,
    max_stocks: int = 20,
    write_file: bool = True,
    available_cash_cny: float | None = None,
    use_portfolio_cash: bool = True,
) -> Dict[str, Any]:
    items = fetch_all(
        queries=["A股 热点 板块", "今日 A股 题材"],
        domestic=True,
        max_per_source=max_news,
        extract_fulltext=False,
        timeout_sec=35,
    )
    enriched = enrich_news_items(items)

    # 从新闻文本中提取股票代码（增强 NLP）
    for item in enriched:
        text = f"{item.title} {item.snippet} {item.text}"
        extracted = extract_stock_symbols_from_text(text)
        if extracted:
            meta = dict(item.raw_meta or {})
            meta["extracted_symbols"] = extracted
            enriched[enriched.index(item)] = RawNewsItem(
                src_name=item.src_name, title=item.title, url=item.url,
                snippet=item.snippet, text=item.text,
                published_at=item.published_at, fetched_at=item.fetched_at,
                raw_meta=meta,
            )

    sector_fund_flows = _fetch_sector_fund_flows()
    flow_items = _fund_flow_items(sector_fund_flows)

    # 北向个股资金流（大幅净买入标的）
    northbound_stocks = _fetch_northbound_individual_flow(max_stocks=15)
    northbound_items = _northbound_flow_items(northbound_stocks)

    combined_items = [*enriched, *flow_items, *northbound_items]
    history = _load_candidate_history(combined_items)
    fundamentals = _load_candidate_fundamentals(combined_items)
    cash_constraint = available_cash_cny
    if cash_constraint is None and use_portfolio_cash:
        cash_constraint = _load_portfolio_cash_cny()
    result = build_daily_selection(
        combined_items,
        history,
        trade_date=trade_date,
        max_stocks=max_stocks,
        available_cash_cny=cash_constraint,
        sector_fund_flows=sector_fund_flows,
        fundamentals_by_symbol=fundamentals,
    )
    payload = result_to_dict(result)
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["source_news_count"] = len(enriched)
    payload["sector_fund_flow_count"] = len(sector_fund_flows)
    payload["northbound_stock_count"] = len(northbound_stocks)
    payload["cash_constraint_cny"] = cash_constraint
    payload["cash_constraint_source"] = _cash_constraint_source(cash_constraint)
    payload["sector_fund_flow_status"] = _sector_fund_flow_status(sector_fund_flows)
    if write_file:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        date_part = payload.get("trade_date") or datetime.now().strftime("%Y-%m-%d")
        slot_part = datetime.now().strftime("%H%M")
        out_path = OUT_DIR / f"selection_{date_part}_{slot_part}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        latest_path = OUT_DIR / "latest.json"
        latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["output_path"] = str(out_path)
        payload["latest_path"] = str(latest_path)
    return payload


def _load_portfolio_cash_cny() -> float | None:
    try:
        from core.portfolio_manager import PortfolioManager

        cash = PortfolioManager().cash_amount("CNY")
        if cash > 0:
            return cash
    except Exception:
        pass
    return _load_monitor_config_cash_cny()


def _load_monitor_config_cash_cny() -> float | None:
    try:
        config = json.loads(MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        config = json.loads(MONITOR_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    cash = _safe_float(config.get("cash"), default=-1.0)
    return cash if cash >= 0 else None


def _cash_constraint_source(cash_constraint: float | None) -> str:
    if cash_constraint is None:
        return "disabled_no_cash_source"
    return "portfolio_or_monitor_config"


def _load_candidate_history(items: Iterable[RawNewsItem]) -> Dict[str, Any]:
    symbols = _candidate_symbols(items)
    history: Dict[str, Any] = {}
    for symbol in symbols:
        try:
            df = get_history_data(symbol, "3mo")
        except Exception:
            continue
        if df is not None and not df.empty:
            history[symbol] = df
    return history


def _load_candidate_fundamentals(items: Iterable[RawNewsItem]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        from core.fundamental_model import assess_fundamentals
        from utils.fundamental_data import get_fundamental_snapshot
    except Exception:
        return out
    for item in items:
        for sector in (item.raw_meta or {}).get("sectors", []) or []:
            sector_name = str(sector.get("sector") or "")
            for leader in sector.get("leaders", []) or []:
                symbol = str(leader.get("symbol") or "").strip()
                if not _is_a_share(symbol) or symbol in out:
                    continue
                name = str(leader.get("name") or symbol)
                try:
                    snapshot = get_fundamental_snapshot(symbol, "a")
                    assessment = assess_fundamentals(
                        symbol=symbol,
                        name=name,
                        sector=sector_name,
                        market="a",
                        metrics=snapshot.get("metrics") or {},
                    )
                    out[symbol] = {
                        "score": assessment.score,
                        "model": assessment.model_key,
                        "reason": f"基本面 {assessment.score:.0f}，覆盖度 {assessment.coverage:.0%}",
                    }
                except Exception:
                    continue
    return out


def _fetch_sector_fund_flows(*, max_sectors: int = 8, max_leaders: int = 5) -> List[Dict[str, Any]]:
    rows = _fetch_legacy_sector_fund_flows(max_sectors=max_sectors, max_leaders=max_leaders)
    if rows:
        return rows
    rows = _fetch_fallback_sector_fund_flows(max_sectors=max_sectors, max_leaders=max_leaders)
    if rows:
        return rows
    # 最后降级到浏览器爬虫
    try:
        from utils.browser_scraper import fetch_sector_flow_browser
        return fetch_sector_flow_browser(max_sectors=max_sectors)
    except Exception:
        return []


def _fetch_legacy_sector_fund_flows(*, max_sectors: int = 8, max_leaders: int = 5) -> List[Dict[str, Any]]:
    try:
        import akshare as ak

        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
    except Exception:
        return []
    if df is None or df.empty:
        return []
    rows: List[Dict[str, Any]] = []
    for index, (_, row) in enumerate(df.head(max_sectors).iterrows(), start=1):
        sector = str(_pick(row, ("名称", "行业", "板块", "板块名称", "name")) or "").strip()
        if not sector:
            continue
        flow = {
            "rank": int(_safe_float(_pick(row, ("序号", "排名", "rank")), index) or index),
            "sector": sector,
            "change_pct": _safe_float(_pick(row, ("今日涨跌幅", "涨跌幅", "板块涨跌幅", "change_pct"))),
            "main_net_inflow_cny": _safe_float(_pick(row, ("今日主力净流入-净额", "主力净流入-净额", "主力净流入", "净额", "net_inflow_cny"))),
            "fund_flow_source": "stock_sector_fund_flow_rank",
            "leaders": _fetch_sector_flow_leaders(sector, max_leaders=max_leaders),
        }
        rows.append(flow)
    return rows


def _fetch_fallback_sector_fund_flows(*, max_sectors: int = 8, max_leaders: int = 5) -> List[Dict[str, Any]]:
    try:
        import akshare as ak
    except Exception:
        return []

    frames: List[tuple[str, Any]] = []
    for source_name, fn_name in (
        ("stock_fund_flow_industry", "stock_fund_flow_industry"),
        ("stock_fund_flow_concept", "stock_fund_flow_concept"),
    ):
        try:
            df = getattr(ak, fn_name)()
        except Exception:
            continue
        if df is not None and not df.empty:
            frames.append((source_name, df))

    candidates: List[Dict[str, Any]] = []
    for source_name, df in frames:
        for index, (_, row) in enumerate(df.iterrows(), start=1):
            sector = str(_pick(row, ("行业", "名称", "板块", "板块名称", "name")) or "").strip()
            if not sector:
                continue
            net = _fund_flow_amount_to_cny(_pick(row, ("净额", "今日主力净流入-净额", "主力净流入", "net_inflow_cny")))
            candidates.append(
                {
                    "rank": index,
                    "sector": sector,
                    "change_pct": _safe_float(_pick(row, ("行业-涨跌幅", "今日涨跌幅", "涨跌幅", "change_pct"))),
                    "main_net_inflow_cny": net,
                    "fund_flow_source": source_name,
                    "leaders": _fallback_flow_leaders(row, sector, max_leaders=max_leaders),
                }
            )
    candidates.sort(key=lambda item: (-_safe_float(item.get("main_net_inflow_cny")), int(item.get("rank") or 999)))
    return candidates[:max_sectors]


def _fetch_sector_flow_leaders(sector: str, *, max_leaders: int = 5) -> List[Dict[str, Any]]:
    try:
        import akshare as ak

        df = ak.stock_sector_fund_flow_summary(symbol=sector, indicator="今日")
    except Exception:
        return []
    if df is None or df.empty:
        return []
    leaders: List[Dict[str, Any]] = []
    for index, (_, row) in enumerate(df.head(max_leaders).iterrows(), start=1):
        symbol = str(_pick(row, ("代码", "股票代码", "symbol")) or "").strip()
        if not _is_a_share(symbol):
            continue
        leaders.append(
            {
                "symbol": symbol,
                "name": str(_pick(row, ("名称", "股票名称", "name")) or symbol),
                "reason": f"{sector}板块内资金流排名第 {index}",
                "main_net_inflow_cny": _safe_float(_pick(row, ("今日主力净流入-净额", "主力净流入-净额", "主力净流入", "净额", "net_inflow_cny"))),
                "change_pct": _safe_float(_pick(row, ("今日涨跌幅", "涨跌幅", "change_pct"))),
            }
        )
    return leaders


def _fallback_flow_leaders(row: Any, sector: str, *, max_leaders: int = 5) -> List[Dict[str, Any]]:
    name = str(_pick(row, ("领涨股", "名称", "股票名称", "name")) or "").strip()
    if not name:
        return []
    return [
        {
            "symbol": "",
            "name": name,
            "reason": f"{sector}板块资金流备用源领涨股",
            "main_net_inflow_cny": _fund_flow_amount_to_cny(_pick(row, ("净额", "主力净流入", "net_inflow_cny"))),
            "change_pct": _safe_float(_pick(row, ("领涨股-涨跌幅", "涨跌幅", "change_pct"))),
        }
    ][:max_leaders]


def _fund_flow_amount_to_cny(value: Any) -> float:
    amount = _safe_float(value)
    if abs(amount) < 10000:
        return amount * 100_000_000.0
    if abs(amount) < 1_000_000:
        return amount * 10_000.0
    return amount


def _sector_fund_flow_status(flows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not flows:
        return {
            "available": False,
            "source": None,
            "message": "未获取到板块资金流，选股未纳入大资金流向权重",
        }
    sources = sorted({str(flow.get("fund_flow_source") or "unknown") for flow in flows})
    return {
        "available": True,
        "source": ",".join(sources),
        "message": f"已纳入 {len(flows)} 个板块的大资金流向",
    }


def _fund_flow_items(flows: List[Dict[str, Any]]) -> List[RawNewsItem]:
    items: List[RawNewsItem] = []
    for flow in flows:
        sector = flow.get("sector")
        leaders = flow.get("leaders") or []
        if not sector or not leaders:
            continue
        items.append(
            RawNewsItem(
                src_name="sector_fund_flow",
                title=f"{sector}板块主力资金净流入靠前",
                url="akshare://stock_sector_fund_flow_rank",
                snippet="大资金流向板块信号",
                raw_meta={
                    "hot_score": max(10_000, abs(_safe_float(flow.get("main_net_inflow_cny"))) / 1000),
                    "sectors": [
                        {
                            "sector": sector,
                            "leaders": leaders,
                        }
                    ],
                },
            )
        )
    return items


def _fetch_northbound_individual_flow(max_stocks: int = 10) -> List[Dict[str, Any]]:
    """获取北向资金个股净买入 TOP 排名（先 akshare，失败用浏览器）"""
    # 1. 先试 akshare
    try:
        import akshare as ak
        df = ak.stock_hsgt_stock_statistics_em(symbol='沪股通')
        if df is not None and not df.empty:
            results = []
            for _, row in df.head(max_stocks).iterrows():
                symbol = str(row.get("代码", row.get("股票代码", ""))).strip()
                name = str(row.get("名称", row.get("股票名称", symbol))).strip()
                net_buy = _safe_float(row.get("净买额") or row.get("今日净买额") or 0)
                change_pct = _safe_float(row.get("涨跌幅") or 0)
                if net_buy > 0 and symbol:
                    results.append({
                        "symbol": symbol, "name": name, "market": "A股",
                        "net_buy_cny": net_buy, "change_pct": change_pct,
                        "source": "akshare_hsgt",
                        "reason": f"沪股通净买入 {net_buy/10000:,.0f}万",
                    })
            if results:
                return results[:max_stocks]
    except Exception:
        pass

    # 2. 降级到浏览器爬虫
    try:
        from utils.browser_scraper import fetch_northbound_stocks_browser
        return fetch_northbound_stocks_browser(max_stocks=max_stocks)
    except Exception as e:
        log.warning("北向个股数据获取失败: %s", e)
        return []


def _northbound_flow_items(stocks: List[Dict[str, Any]]) -> List[RawNewsItem]:
    """将北向个股净买数据转为 RawNewsItem，注入选股池"""
    if not stocks:
        return []
    leaders = [
        {"symbol": s["symbol"], "name": s["name"], "market": "A股", "reason": s["reason"]}
        for s in stocks
    ]
    return [
        RawNewsItem(
            src_name="northbound_flow",
            title=f"北向资金今日大幅净买入 {len(stocks)} 只A股",
            url="akshare://stock_hsgt_stock_statistics_em",
            snippet="; ".join(f"{s['name']}({s['symbol']}) 净买{s['net_buy_cny']/10000:,.0f}万" for s in stocks[:5]),
            raw_meta={
                "hot_score": max(10_000, sum(s["net_buy_cny"] for s in stocks) / 1000),
                "sectors": [{"sector": "北向资金", "leaders": leaders}],
            },
        )
    ]


def _pick(row: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        if name in row:
            return row[name]
    for key, value in getattr(row, "items", lambda: [])():
        key_text = str(key)
        if any(name in key_text for name in names):
            return value
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            if not text or text in {"-", "--"}:
                return default
            multiplier = 1.0
            if text.endswith("亿"):
                multiplier = 100_000_000.0
                text = text[:-1]
            elif text.endswith("万"):
                multiplier = 10_000.0
                text = text[:-1]
            if text.endswith("%"):
                text = text[:-1]
            return float(text) * multiplier
        return float(value)
    except (TypeError, ValueError):
        return default


def _candidate_symbols(items: Iterable[RawNewsItem]) -> List[str]:
    out: List[str] = []
    for item in items:
        # From sector leaders
        for sector in (item.raw_meta or {}).get("sectors", []) or []:
            for leader in sector.get("leaders", []) or []:
                symbol = str(leader.get("symbol") or "").strip()
                if _is_a_share(symbol) and symbol not in out:
                    out.append(symbol)
        # From stock code extraction in news text
        for extracted in (item.raw_meta or {}).get("extracted_symbols", []) or []:
            symbol = str(extracted.get("symbol") or "").strip()
            if _is_a_share(symbol) and symbol not in out:
                out.append(symbol)
    return out


def _is_a_share(symbol: str) -> bool:
    return symbol.isdigit() and len(symbol) == 6 and symbol[0] in {"0", "2", "3", "4", "6", "8"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build daily A-share stock selection JSON")
    parser.add_argument("--date", dest="trade_date", help="Trade date YYYY-MM-DD; defaults to latest bar")
    parser.add_argument("--max-news", type=int, default=30)
    parser.add_argument("--max-stocks", type=int, default=20)
    parser.add_argument("--cash-cny", type=float, default=None, help="Override CNY cash constraint")
    parser.add_argument("--ignore-cash", action="store_true", help="Do not filter by portfolio cash")
    parser.add_argument("--no-write", action="store_true", help="Print JSON only; do not write data/")
    args = parser.parse_args()
    payload = run_daily_stock_selection(
        trade_date=args.trade_date,
        max_news=args.max_news,
        max_stocks=args.max_stocks,
        write_file=not args.no_write,
        available_cash_cny=args.cash_cny,
        use_portfolio_cash=not args.ignore_cash,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
