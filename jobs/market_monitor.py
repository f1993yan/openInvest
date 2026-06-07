"""盘中行情监控 + 委员会分析

每 30 分钟（9:30-15:00）自动拉行情、跑委员会、通知重点操作标的。

启动方式:
  python -m jobs.market_monitor
  或从 start-invest-backend.bat 一并启动
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# 确保项目根在 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

REPORT_DIR = _PROJECT_ROOT / "data" / "market_monitor"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("market_monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [market_monitor] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(REPORT_DIR / "monitor.log", encoding="utf-8"),
    ],
)

# ==========================================
# 配置
# ==========================================

BACKEND_URL = os.getenv("INVEST_BACKEND_URL", "http://127.0.0.1:8766")
CONFIG_PATH = Path(__file__).with_name("market_monitor_config.json")

# 交易时间（北京时间）
TRADING_START = dt_time(9, 30)
LUNCH_START = dt_time(11, 30)
LUNCH_END = dt_time(13, 0)
TRADING_END = dt_time(14, 50)  # 14:50 末轮盯盘，不等到15:00
INTERVAL_MINUTES = 30

# ==========================================
# 行情拉取
# ==========================================


def fetch_sina_prices(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """从新浪 API 拉取最新行情

    Returns:
        {symbol: {"price": float, "prev_close": float, "change_pct": float, "name": str}}
    """
    sina_map = {
        "600900": "sh600900", "601138": "sh601138", "600487": "sh600487",
        "600183": "sh600183", "601179": "sh601179", "301377": "sz301377",
        "600580": "sh600580", "688099": "sh688099", "000063": "sz000063",
        "002185": "sz002185", "300476": "sz300476", "00700": "hk00700",
        "09988": "hk09988",
    }

    # 自动补充新浪前缀（sz/sh/hk），兜底未知symbol
    def _to_sina(sym):
        if sym in sina_map:
            return sina_map[sym]
        if sym.isdigit() and len(sym) == 6:
            return f"sh{sym}" if sym[0] == "6" else f"sz{sym}"
        if sym.isdigit() and len(sym) == 5:
            return f"hk{sym}"
        return sym

    sina_list = ",".join(_to_sina(s) for s in symbols)
    url = f"https://hq.sinajs.cn/list={sina_list}"

    # 重试3次，应对新浪API偶发抖动
    text = ""
    for attempt in range(3):
        try:
            resp = requests.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=15)
            resp.encoding = "gbk"
            text = resp.text
            if text and 'hq_str_' in text:
                break
            log.warning(f"新浪行情第{attempt+1}次返回空数据，重试...")
            time.sleep(2)
        except Exception as e:
            log.warning(f"新浪行情第{attempt+1}次失败: {e}")
            time.sleep(2)
    if not text or 'hq_str_' not in text:
        log.error(f"新浪行情3次重试均失败")
        return {}

    results = {}
    for line in text.strip().split("\n"):
        if not line.strip() or "=" not in line:
            continue
        try:
            var_name, data = line.split("=", 1)
            # 从 var_name 提取 symbol
            # 格式: var hq_str_sh600900="..."
            symbol_part = var_name.replace("var hq_str_", "").strip()
            # symbol_part like "sh600900" → "600900"
            if symbol_part.startswith("sh"):
                symbol = symbol_part[2:]
            elif symbol_part.startswith("sz"):
                symbol = symbol_part[2:]
            elif symbol_part.startswith("hk"):
                symbol = symbol_part[2:]
            else:
                symbol = symbol_part

            data = data.strip('";\n ')
            fields = data.split(",")

            if len(fields) < 4:
                continue

            name = fields[0]
            # 港股格式不同
            if "hk" in var_name.lower():
                # hk: name, english_name, open, prev_close, price, high, low, ...
                if len(fields) >= 10:
                    prev_close = float(fields[3]) if fields[3] else 0
                    price = float(fields[5]) if fields[5] else 0
                    change_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0
                else:
                    continue
            else:
                # A股: name, open, prev_close, price, high, low, ...
                if len(fields) >= 4:
                    prev_close = float(fields[2]) if fields[2] else 0
                    price = float(fields[3]) if fields[3] else 0
                    change_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0
                else:
                    continue

            results[symbol] = {
                "name": name,
                "price": price,
                "prev_close": prev_close,
                "change_pct": round(change_pct, 2),
            }
        except Exception as e:
            log.warning(f"解析行情行失败: {line[:60]}... {e}")

    return results


# ==========================================
# 委员会 API 调用
# ==========================================


def call_committee(symbol: str, name: str, market: str,
                   position_pct: float, cost: float, current_price: float,
                   total_assets: float, cash: float,
                   all_holdings: List[Dict],
                   target_position_pct: Optional[float] = None,
                   t2_pending: float = 0.0,
                   news_brief: str = "",
                   sector: str = "",
                   industry: str = "",
                   fundamentals: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
    """调用后端委员会 API"""
    payload = {
        "symbol": symbol,
        "name": name,
        "market": market,
        "sector": sector,
        "industry": industry,
        "position_pct": position_pct,
        "target_position_pct": target_position_pct,
        "cost": cost,
        "current_price": current_price,
        "total_assets": total_assets,
        "cash": cash,
        "holdings": all_holdings,
        "min_lot_size": 100 if market == "a" else 100,
        "t_plus_1": market == "a",
        "available_cash": cash,
        "t2_pending_cash": t2_pending,
        "news_brief": news_brief,
        "fundamentals": fundamentals or {},
        "max_debate_rounds": 2,  # 监控模式减半辩论轮数，加速
    }
    try:
        resp = requests.post(
            f"{BACKEND_URL}/api/committee",
            json=payload,
            timeout=120,
        )
        return resp.json()
    except Exception as e:
        log.error(f"委员会 API {symbol} 失败: {e}")
        return {"success": False, "symbol": symbol, "error": str(e)}


# ==========================================
# 通知
# ==========================================


def send_windows_toast(title: str, body: str):
    """Windows 通知 — 使用 ctypes MessageBox（非阻塞线程）"""
    import ctypes
    import threading

    def _show():
        try:
            ctypes.windll.user32.MessageBoxW(
                0, body, title,
                0x40 | 0x40000,  # MB_ICONINFORMATION | MB_TOPMOST
            )
        except Exception:
            pass

    threading.Thread(target=_show, daemon=True).start()


def write_report(round_time: str, results: List[Dict],
                 actionable: List[Dict], prices: Dict,
                 news_items: List = None):
    """写入监控报告"""
    report_path = REPORT_DIR / f"monitor_{round_time.replace(':', '-')}.md"

    lines = [
        f"# 📊 盘中监控报告 — {round_time}",
        "",
        "## 📈 最新行情",
        "",
        "| 股票 | 代码 | 最新价 | 涨跌幅 |",
        "|------|------|--------|--------|",
    ]
    for sym, p in sorted(prices.items()):
        lines.append(f"| {p['name']} | {sym} | ¥{p['price']:.2f} | {p['change_pct']:+.2f}% |")

    # 新闻摘要
    if news_items:
        from collections import Counter
        src_counts = Counter(i.src_name for i in news_items)
        lines.extend([
            "",
            "## 📰 国内新闻摘要",
            "",
            f"共 {len(news_items)} 条（{', '.join(f'{k}:{v}' for k,v in sorted(src_counts.items()))}）",
            "",
        ])
        sector_summary = []
        try:
            from services.news_sources.domestic_hot_news import summarize_sector_hits
            sector_summary = summarize_sector_hits(news_items)
        except Exception as e:
            log.warning(f"新闻板块汇总失败: {e}")

        if sector_summary:
            lines.extend([
                "### 涉及板块与龙头",
                "",
                "| 板块 | 热度 | 龙头股票 | 新闻源 |",
                "|------|------|----------|--------|",
            ])
            for row in sector_summary:
                leaders = "、".join(
                    f"{leader.get('name')}({leader.get('symbol')})"
                    for leader in row.get("leaders", [])
                    if leader.get("name") and leader.get("symbol")
                )
                lines.append(
                    f"| {row.get('sector')} | {row.get('count')} | {leaders or '-'} | "
                    f"{', '.join(row.get('sources', []))} |"
                )
            lines.append("")
            lines.append("### 新闻条目")
            lines.append("")

        try:
            from services.news_sources.news_risk_model import summarize_risk_impacts
            risk_rows = summarize_risk_impacts(news_items)
        except Exception as e:
            log.warning(f"新闻风险量化失败: {e}")
            risk_rows = []

        if risk_rows:
            lines.extend([
                "### 重大危险信息量化影响",
                "",
                "| 新闻源 | 风险类别 | CSI300冲击 | 90%区间 | 最大板块冲击 | 模型证据 |",
                "|--------|----------|------------|---------|--------------|----------|",
            ])
            for row in risk_rows:
                ci = row.get("ci90_bps", ["?", "?"])
                top_sector = (row.get("sector_impacts") or [{}])[0]
                sector_text = (
                    f"{top_sector.get('sector')} {top_sector.get('impact_bps'):+.1f}bp"
                    if top_sector.get("sector") else "-"
                )
                lines.append(
                    f"| {row.get('src_name')} | {row.get('category')} | "
                    f"{row.get('csi300_impact_bps'):+.1f}bp | "
                    f"{ci[0]:+.1f}~{ci[1]:+.1f}bp | {sector_text} | "
                    f"{', '.join(row.get('evidence_keywords', [])[:4])} |"
                )
            lines.extend([
                "",
                "> 模型: event_study_prior_capm_v1，公式 = -base_bps(category) * severity * source_reliability * a_share_proximity。",
                "",
            ])

        for item in news_items[:10]:
            title = item.title[:80] if item.title else ""
            if not title:
                continue
            lines.append(f"- [{item.src_name}] {title}")
            for sector in (item.raw_meta or {}).get("sectors", [])[:2]:
                leaders = "、".join(
                    f"{leader.get('name')}({leader.get('symbol')})"
                    for leader in sector.get("leaders", [])[:3]
                    if leader.get("name") and leader.get("symbol")
                )
                if leaders:
                    lines.append(f"  - 涉及板块: {sector.get('sector')}；对应龙头: {leaders}")
            for impact in (item.raw_meta or {}).get("risk_impacts", [])[:1]:
                ci = impact.get("ci90_bps", ["?", "?"])
                lines.append(
                    f"  - 危险信号: {impact.get('category')}；"
                    f"CSI300模型冲击 {impact.get('csi300_impact_bps'):+.1f}bp "
                    f"(90%区间 {ci[0]:+.1f}~{ci[1]:+.1f}bp)"
                )

    lines.extend([
        "",
        "## 🏛️ 委员会分析汇总",
        "",
        "| 股票 | Verdict | 置信度 | 建议 | Fundamental | Quant | Regime |",
        "|------|---------|--------|------|-------------|-------|--------|",
    ])
    for r in results:
        if r.get("success"):
            fundamental = r.get("fundamental_model", "")
            if r.get("fundamental_score") is not None:
                fundamental = f"{fundamental}:{r.get('fundamental_score', 50):.0f}"
            lines.append(
                f"| {r['name']} | **{r['verdict']}** | {r['confidence']:.2f} | "
                f"¥{r.get('suggested_alloc_cny', 0):,.0f} | "
                f"{fundamental} | "
                f"{r.get('quant_view', '')[:30]}... | "
                f"{r.get('regime', '')[:20]}... |"
            )
        else:
            lines.append(f"| {r.get('name', r.get('symbol'))} | ❌ 失败 | — | — | — | — | — |")

    if actionable:
        lines.extend([
            "",
            "## 🎯 ⚠️ 需要操作的标的",
            "",
        ])
        for i, a in enumerate(actionable, 1):
            lines.append(
                f"### {i}. {a['name']} ({a['symbol']}) — **{a['verdict']}** "
                f"(置信度: {a['confidence']:.2f})"
            )
            if a.get("cio_memo"):
                # 提取 PERSONAL_NOTE 段落
                memo = a["cio_memo"]
                if "PERSONAL_NOTE:" in memo:
                    note = memo.split("PERSONAL_NOTE:")[1].split("\n\n")[0]
                    lines.append(f"")
                    lines.append(f"> {note.strip()}")
                elif "EXECUTION_PLAN:" in memo:
                    plan = memo.split("EXECUTION_PLAN:")[1].split("RISK_PLAN:")[0]
                    lines.append(f"```")
                    lines.append(f"EXECUTION_PLAN:{plan.strip()}")
                    lines.append(f"```")
            lines.append("")

    lines.extend([
        "---",
        f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
    ])

    report_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"报告已写入: {report_path}")

    # 同时写入最新摘要
    latest_path = REPORT_DIR / "latest.md"
    latest_path.write_text("\n".join(lines), encoding="utf-8")


# ==========================================
# 主循环
# ==========================================


def is_trading_time() -> bool:
    """判断当前是否在交易时段内（周一至周五 9:30-11:30, 13:00-15:00）"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周六日
        return False
    t = now.time()
    return (TRADING_START <= t <= LUNCH_START) or (LUNCH_END <= t <= TRADING_END)


def wait_until_next_round():
    """等待到下一个整30分钟"""
    now = datetime.now()
    minute = now.minute
    # 计算下一个 0 或 30 分钟点
    if minute < 30:
        next_minute = 30
        next_hour = now.hour
    else:
        next_minute = 0
        next_hour = now.hour + 1

    target = now.replace(hour=next_hour, minute=next_minute, second=0, microsecond=0)
    # 如果下一个整点在收盘后，强制改为14:50（末轮盯盘）
    target_end = now.replace(hour=TRADING_END.hour, minute=TRADING_END.minute, second=0, microsecond=0)
    if target > target_end:
        target = target_end
    wait_sec = (target - now).total_seconds()
    if wait_sec > 0:
        log.info(f"等待 {wait_sec / 60:.1f} 分钟到 {target.strftime('%H:%M')}")
        time.sleep(wait_sec)


def load_config() -> Dict:
    """加载持仓配置"""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        log.error(f"配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)


def build_holdings_list(holdings: List[Dict], exclude_symbol: str) -> List[Dict]:
    """构建 holdings 列表（排除当前分析的标的）"""
    return [
        {
            "symbol": h["symbol"],
            "name": h["name"],
            "weight_pct": h["position_pct"],
            "cost": h["cost"],
        }
        for h in holdings
        if h["symbol"] != exclude_symbol
    ]


def _sync_config_account_fields(config: Dict, account_stocks: List[Dict]) -> Dict:
    """Return a monitor config view backed by real-account holdings."""
    updated = dict(config)
    by_symbol = {s["symbol"]: s for s in account_stocks}
    updated["holdings"] = [
        by_symbol.get(s.get("symbol"), s)
        for s in config.get("holdings", [])
    ]
    updated["watchlist"] = [
        by_symbol.get(s.get("symbol"), s)
        for s in config.get("watchlist", [])
    ]
    return updated


def run_monitor_round():
    """执行一轮监控"""
    round_time = datetime.now().strftime("%H:%M")
    log.info(f"=== 开始监控轮次 {round_time} ===")

    config = load_config()
    try:
        from db.account_ledger import AccountLedger, REAL_ACCOUNT
        ledger = AccountLedger()
        ledger.ensure_initialized(config)
        account_stocks = ledger.stocks_for_committee_input(config, account=REAL_ACCOUNT)
        config = _sync_config_account_fields(config, account_stocks)
    except Exception as e:
        ledger = None
        log.warning(f"双账户账本初始化/读取失败，回退配置持仓: {e}")

    holdings = config.get("holdings", [])
    watchlist = config.get("watchlist", [])
    total_assets = config["total_assets"]
    cash = config["cash"]
    if ledger is not None:
        try:
            real_summary = ledger.account_summary("real")
            cash = float(real_summary.get("cash_cny", cash) or cash)
        except Exception:
            pass

    all_stocks = holdings + watchlist
    all_symbols = [s["symbol"] for s in all_stocks]

    # 1. 拉取行情
    log.info(f"拉取 {len(all_symbols)} 只标的最新行情...")
    prices = fetch_sina_prices(all_symbols)
    log.info(f"获取到 {len(prices)} 只标的价格")

    # 1.5 拉取国内新闻
    news_items = []
    try:
        sys.path.insert(0, str(_PROJECT_ROOT))
        from services.news_sources import fetch_all
        news_items = fetch_all(domestic=True, max_per_source=8, timeout_sec=20)
        log.info(f"新闻: {len(news_items)} 条")
    except Exception as e:
        log.warning(f"新闻拉取失败: {e}")

    # 2. 逐只跑委员会
    results = []
    for stock in all_stocks:
        sym = stock["symbol"]
        price_info = prices.get(sym)

        if not price_info or price_info["price"] <= 0:
            log.warning(f"{sym} 行情缺失，跳过")
            results.append({"success": False, "symbol": sym, "name": stock["name"],
                            "error": "行情缺失"})
            continue

        # 构建 holdings（排除当前标的）
        other_holdings = build_holdings_list(holdings, sym)

        # 构建新闻摘要文本
        news_text = ""
        if news_items:
            try:
                from services.news_sources.domestic_hot_news import format_sector_brief
                news_text = format_sector_brief(news_items, max_items=8)
            except Exception:
                lines = []
                for item in news_items[:8]:
                    title = item.title[:100] if item.title else ""
                    if title:
                        lines.append(f"- [{item.src_name}] {title}")
                news_text = "\n".join(lines)

        result = call_committee(
            symbol=sym,
            name=stock["name"],
            market=stock.get("market", "a"),
            position_pct=stock.get("position_pct", 0),
            cost=stock.get("cost", 0),
            current_price=price_info["price"],
            total_assets=total_assets,
            cash=cash,
            all_holdings=other_holdings,
            target_position_pct=stock.get("target_position_pct", stock.get("target_pct")),
            t2_pending=config.get("t2_pending_cash", 0),
            news_brief=news_text,
            sector=stock.get("sector", ""),
            industry=stock.get("industry", ""),
            fundamentals=stock.get("fundamentals", {}),
        )
        if result and result.get("success"):
            result.setdefault("symbol", sym)
            result.setdefault("name", stock["name"])
            if ledger is not None:
                try:
                    trade = ledger.apply_committee_result(result, price=price_info["price"])
                    if trade:
                        log.info(
                            f"影子账户执行 {trade.direction} {trade.symbol} "
                            f"{trade.units:.0f}股 @{trade.price:.2f}"
                        )
                except Exception as e:
                    log.warning(f"影子账户执行委员会建议失败 {sym}: {e}")
        results.append(result)

        # 打印简要结果
        if result.get("success"):
            v = result["verdict"]
            conf = result["confidence"]
            alloc = result.get("suggested_alloc_cny", 0)
            log.info(f"  {sym} {stock['name']}: {v} (conf={conf:.2f}, alloc=¥{alloc:,.0f})")
        else:
            log.warning(f"  {sym} {stock['name']}: 分析失败 - {result.get('error', 'unknown')}")

        # 避免过快连续请求
        time.sleep(1)

    # 3. 筛选需要操作的标的
    actionable = [
        r for r in results
        if r.get("success")
        and r.get("verdict") not in ("HOLD", "REDUCE", "UNCLEAR")
        and abs(r.get("suggested_alloc_cny", 0) or 0) > 0
        and r.get("confidence", 0) >= 0.55
    ]

    # 4. 写报告
    write_report(round_time, results, actionable, prices, news_items)
    if ledger is not None:
        try:
            from db.account_ledger import prices_from_sina_result
            pnl_rows = ledger.snapshot_daily_pnl(prices=prices_from_sina_result(prices))
            for row in pnl_rows:
                log.info(
                    f"账户PnL {row['account']}: total={row['total_value_cny']:,.0f}, "
                    f"day={row['day_pnl_cny']}, pnl={row['total_pnl_pct']:+.2f}%"
                )
        except Exception as e:
            log.warning(f"双账户收盘/PnL快照失败: {e}")

    # 5. 通知 — 始终弹窗，包含完整监控摘要
    import re as _re
    toast_lines = []

    # 新闻源摘要（前3条）
    if news_items:
        toast_lines.append("── 📰 新闻 ──")
        for item in news_items[:3]:
            title = item.title[:60] if item.title else ""
            if title:
                toast_lines.append(f"  [{item.src_name}] {title}")
                sectors = (item.raw_meta or {}).get("sectors", [])
                if sectors:
                    leaders = sectors[0].get("leaders", [])
                    leader = leaders[0] if leaders else {}
                    if leader.get("name"):
                        toast_lines.append(
                            f"    {sectors[0].get('sector')} → {leader.get('name')}({leader.get('symbol')})"
                        )

    # actionable 标的
    if actionable:
        toast_lines.append("── ⚠️ 需操作 ──")
        for a in actionable[:4]:
            memo = a.get("cio_memo", "")
            # 解析止盈止损
            sl = _re.search(r"stop_loss_price:\s*([\d.]+)", memo)
            tp = _re.search(r"take_profit_price:\s*([\d.]+)", memo)
            sl_str = f" 损¥{sl.group(1)}" if sl else ""
            tp_str = f" 盈¥{tp.group(1)}" if tp else ""
            # 手数
            alloc = a.get("suggested_alloc_cny", 0)
            price = prices.get(a.get("symbol", ""), {}).get("price", 1)
            hands = int(alloc / (price * 100)) if price > 0 and alloc > 0 else 0
            hands_str = f" {hands}手" if hands > 0 else ""
            v_map = {"ACCUMULATE": "🟢↑买", "BUY": "🟢↑买", "TRIM": "🔴↓卖", "SELL": "🔴↓卖", "HOLD": "⚪→持"}
            v_icon = v_map.get(a['verdict'], a['verdict'])
            toast_lines.append(
                f"  {v_icon} {a['name']}{hands_str} "
                f"¥{alloc:,.0f}{sl_str}{tp_str} "
                f"(c={a['confidence']:.2f})"
            )
    else:
        toast_lines.append("── ✅ 无需操作 ──")

    # 持仓标的快照（价格 + 涨跌幅 + verdict + 置信度）
    toast_lines.append("── 📊 持仓 ──")
    for r in results[:8]:
        if r.get("success"):
            sym = r.get("symbol", "")
            name = r.get("name", sym)
            v = r.get("verdict", "?")
            conf = r.get("confidence", 0)
            # 从 prices 取涨跌幅和价格
            p = prices.get(sym, {})
            price = p.get("price", 0)
            chg = p.get("change_pct", 0)
            arrow = "↑" if chg > 0 else "↓" if chg < 0 else "→"
            # 解析止盈止损
            memo = r.get("cio_memo", "")
            sl = _re.search(r"stop_loss_price:\s*([\d.]+)", memo) if _re else None
            tp = _re.search(r"take_profit_price:\s*([\d.]+)", memo) if _re else None
            sl_str = f" 损{sl.group(1)}" if sl else ""
            tp_str = f" 盈{tp.group(1)}" if tp else ""
            v_map2 = {"ACCUMULATE": "🟢↑", "BUY": "🟢↑↑", "HOLD": "⚪→", "TRIM": "🔴↓", "SELL": "🔴↓↓"}
            v_icon2 = v_map2.get(v, v)
            toast_lines.append(
                f"  {v_icon2} {name} ¥{price:.0f} {arrow}{abs(chg):.1f}% "
                f"({conf:.2f}){sl_str}{tp_str}"
            )

    toast_body = "\n".join(toast_lines[:25])  # 最多25行
    send_windows_toast(
        f"📊 {round_time} 监控 ({len(actionable)}只需操作)",
        toast_body
    )
    if actionable:
        log.info(f"⚠️ 需要操作: {len(actionable)} 只标的")
    else:
        log.info("✅ 本轮无需操作")


def main():
    """主入口：循环执行监控"""
    log.info("=== OpenInvest 盘中监控启动 ===")
    log.info(f"交易时间: {TRADING_START}-{LUNCH_START}, {LUNCH_END}-{TRADING_END}, 间隔: {INTERVAL_MINUTES}分钟")
    log.info(f"后端: {BACKEND_URL}")

    # 立即执行第一轮
    if is_trading_time():
        log.info("当前在交易时段，立即执行首轮...")
        try:
            run_monitor_round()
        except Exception as e:
            log.exception(f"首轮执行失败: {e}")
    else:
        log.info("当前非交易时段，等待开盘...")

    # 主循环
    while True:
        try:
            if not is_trading_time():
                # 非交易时段，每分钟检查一次
                now = datetime.now()
                if now.weekday() >= 5:
                    # 周末，等到周一
                    days_to_monday = 7 - now.weekday()
                    log.info(f"周末，等待 {days_to_monday} 天到周一...")
                    time.sleep(3600)
                else:
                    log.info(f"非交易时段 ({now.strftime('%H:%M')})，等待中...")
                    time.sleep(60)
                continue

            # 交易时段：等待到下一个30分钟节点
            wait_until_next_round()
            run_monitor_round()

        except KeyboardInterrupt:
            log.info("用户中断，退出监控")
            break
        except Exception as e:
            log.exception(f"监控轮次异常: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
