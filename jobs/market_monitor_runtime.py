"""Runtime loop for the intraday market monitor."""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from jobs.market_monitor_common import (
    CONFIG_PATH,
    INTERVAL_MINUTES,
    LATEST_WINDOW_PATH,
    LUNCH_END,
    LUNCH_START,
    MONITOR_POPUPS_ENABLED,
    TRADING_END,
    TRADING_START,
    _PROJECT_ROOT,
    log,
    _fmt_price,
    _safe_num,
)
from jobs.market_monitor_alerts import select_optimal_actionable_alerts
from jobs.market_monitor_entry_exit import (
    format_entry_exit_alert_body,
    load_entry_exit_alert_state,
    update_entry_exit_alert_state,
)
from jobs.market_monitor_guards import apply_limit_up_guard, apply_repeated_trade_guard
from jobs.market_monitor_notify import is_scheduled_monitor_popup_time, send_windows_toast, should_send_monitor_summary_popup
from jobs.market_monitor_quotes import call_committee, fetch_sina_prices
from jobs.market_monitor_snapshot import build_monitor_window_snapshot, write_monitor_window_snapshot, write_report
from jobs.trading_mode import DEFAULT_TRADING_MODE, normalize_trading_mode

SECTOR_CACHE_PATH = _PROJECT_ROOT / "data" / "sector_cache.json"


def _clean_sector(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"", "unknown", "none", "null", "-"} or text in {"未分组", "全局"} else text


def load_sector_cache_mapping(path: Path = SECTOR_CACHE_PATH) -> Dict[str, str]:
    """Load ignored Eastmoney sector mapping used by weekly optimization."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning(f"东方财富板块缓存读取失败: {exc}")
        return {}
    mapping = data.get("mapping") if isinstance(data, dict) else {}
    if not isinstance(mapping, dict):
        return {}
    return {
        str(symbol).strip(): _clean_sector(sector)
        for symbol, sector in mapping.items()
        if str(symbol).strip() and _clean_sector(sector)
    }


def apply_sector_cache_to_stocks(
    stocks: List[Dict[str, Any]],
    sector_mapping: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Return runtime stock rows whose sector matches weekly Eastmoney mapping.

    This keeps config/ledger files untouched while making committee context,
    position_exit_plan policy lookup, alert distribution, and monitor-window
    display use the same sector key as weekly_exit_param_optimization.py.
    """
    out: List[Dict[str, Any]] = []
    for stock in stocks:
        row = dict(stock)
        symbol = str(row.get("symbol") or "").strip()
        cached_sector = _clean_sector(sector_mapping.get(symbol))
        if cached_sector and str(row.get("market", "a")).lower() == "a":
            original_sector = _clean_sector(row.get("sector"))
            if original_sector and original_sector != cached_sector:
                row.setdefault("config_sector", original_sector)
            row["sector"] = cached_sector
            row.setdefault("sector_source", "eastmoney_sector_cache")
        out.append(row)
    return out

def is_trading_time() -> bool:
    """判断当前是否在交易时段内（交易日 9:30-15:00）"""
    from utils.market_calendar import is_trading_day
    now = datetime.now()
    if not is_trading_day("XSHG", now.date()):
        return False
    t = now.time()
    return TRADING_START <= t <= TRADING_END


def wait_until_next_round():
    """等待到下一个监控间隔边界"""
    now = datetime.now()
    interval = max(1, INTERVAL_MINUTES)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_minutes = now.hour * 60 + now.minute
    next_slot_minutes = ((elapsed_minutes // interval) + 1) * interval
    target = day_start + timedelta(minutes=next_slot_minutes)
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
        for enc in ("utf-8", "utf-8-sig", "gbk", "cp936"):
            try:
                with open(CONFIG_PATH, "r", encoding=enc) as f:
                    return json.load(f)
            except UnicodeDecodeError:
                continue
        with open(CONFIG_PATH, "r", encoding="utf-8", errors="replace") as f:
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


def _with_sector_cache(config: Dict, sector_mapping: Dict[str, str]) -> Dict:
    if not sector_mapping:
        return config
    updated = dict(config)
    updated["holdings"] = apply_sector_cache_to_stocks(list(config.get("holdings", []) or []), sector_mapping)
    updated["watchlist"] = apply_sector_cache_to_stocks(list(config.get("watchlist", []) or []), sector_mapping)
    return updated


def run_monitor_round():
    """执行一轮监控"""
    from jobs.market_monitor_common import load_crawler_settings
    settings = load_crawler_settings()
    if not settings.get("target_refresh_enabled", True):
        log.info("标的刷新被设置关闭，跳过本轮监控。")
        return

    round_dt = datetime.now()
    round_time = round_dt.strftime("%H:%M")
    log.info(f"=== 开始监控轮次 {round_time} ===")

    config = load_config()
    sector_mapping = load_sector_cache_mapping()
    try:
        from db.account_ledger import AccountLedger, REAL_ACCOUNT
        ledger = AccountLedger()
        ledger.ensure_initialized(config)
        account_stocks = ledger.stocks_for_committee_input(config, account=REAL_ACCOUNT)
        config = _sync_config_account_fields(config, account_stocks)
    except Exception as e:
        ledger = None
        log.warning(f"双账户账本初始化/读取失败，回退配置持仓: {e}")

    config = _with_sector_cache(config, sector_mapping)
    holdings = config.get("holdings", [])
    watchlist = config.get("watchlist", [])
    total_assets = config["total_assets"]
    cash = config["cash"]
    t2_pending_cash = float(config.get("t2_pending_cash", 0) or 0)

    shadow_stocks = []
    shadow_cash = cash
    shadow_available_cash = cash
    shadow_t2_pending = t2_pending_cash

    if ledger is not None:
        try:
            real_summary = ledger.account_summary("real")
            cash = float(real_summary.get("cash_cny", cash) or cash)
            t2_pending_cash = float(real_summary.get("t2_pending_cash_cny", t2_pending_cash) or 0)
        except Exception:
            pass
        try:
            shadow_stocks = ledger.stocks_for_committee_input(config, account="committee")
            shadow_stocks = apply_sector_cache_to_stocks(shadow_stocks, sector_mapping)
            committee_summary = ledger.account_summary("committee")
            shadow_cash = float(committee_summary.get("cash_cny", cash) or cash)
            shadow_available_cash = shadow_cash
            shadow_t2_pending = float(committee_summary.get("t2_pending_cash_cny", 0) or 0)
        except Exception as e:
            log.warning(f"获取影子账户持仓/现金失败: {e}")

    all_stocks = holdings + watchlist
    all_symbols = [s["symbol"] for s in all_stocks]
    entry_exit_state_before_round = load_entry_exit_alert_state()

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

        # Get shadow stock details
        shadow_stock = next((s for s in shadow_stocks if s["symbol"] == sym), None)
        shadow_pos_pct = shadow_stock.get("position_pct", 0) if shadow_stock else 0.0
        shadow_cost = shadow_stock.get("cost", 0) if shadow_stock else 0.0
        shadow_other_holdings = build_holdings_list(shadow_stocks, sym) if shadow_stocks else []

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
            t2_pending=t2_pending_cash,
            news_brief=news_text,
            sector=stock.get("sector", ""),
            industry=stock.get("industry", ""),
            fundamentals=stock.get("fundamentals", {}),
            optimizer_review_enabled=config.get("optimizer_review_enabled", True),
            shadow_position_pct=shadow_pos_pct,
            shadow_cost=shadow_cost,
            shadow_cash=shadow_cash,
            shadow_holdings=shadow_other_holdings,
            shadow_available_cash=shadow_available_cash,
            shadow_t2_pending=shadow_t2_pending,
        )
        if result and result.get("success"):
            result.setdefault("symbol", sym)
            result.setdefault("name", stock["name"])
            # 兜底 market：委员会响应理应回传 market，但若某生产端/旧响应漏了，
            # 用 config 里该标的的 market 补上，避免 _is_limit_up_buy_blocked 退化成 "a"
            # 把港股/美股误判 A 股涨停。
            result.setdefault("market", stock.get("market", "a"))
            result = apply_repeated_trade_guard(
                result,
                stock=stock,
                current_price=price_info["price"],
                ledger=ledger,
                entry_exit_state=entry_exit_state_before_round,
            )
            # 涨停追高护栏必须在下单前：改写 verdict→HOLD，使 apply_committee_result
            # 真正拦下影子买单（仅在 select_optimal_actionable_alerts 抑制提醒不够）。
            result = apply_limit_up_guard(result, price_info=price_info)

            # Apply shadow result to ledger if available
            shadow_res = result.get("shadow_result")
            if shadow_res and shadow_res.get("success"):
                shadow_res.setdefault("symbol", sym)
                shadow_res.setdefault("name", stock["name"])
                shadow_res.setdefault("market", stock.get("market", "a"))

                # Apply guards to shadow result
                shadow_res = apply_repeated_trade_guard(
                    shadow_res,
                    stock=shadow_stock or stock,
                    current_price=price_info["price"],
                    ledger=ledger,
                    entry_exit_state=entry_exit_state_before_round,
                )
                shadow_res = apply_limit_up_guard(shadow_res, price_info=price_info)

                if ledger is not None:
                    try:
                        trade = ledger.apply_committee_result(shadow_res, price=price_info["price"])
                        if trade:
                            log.info(
                                f"影子账户执行 {trade.direction} {trade.symbol} "
                                f"{trade.units:.0f}股 @{trade.price:.2f}"
                            )
                        elif shadow_res.get("execution_blocked"):
                            log.info(
                                f"影子执行保护: {sym} {stock['name']} "
                                f"{shadow_res.get('optimizer_verdict_before_guard')} "
                                f"alloc=¥{shadow_res.get('optimizer_alloc_cny_before_guard', 0):,.0f} -> HOLD"
                            )
                    except Exception as e:
                        log.warning(f"影子账户执行委员会建议失败 {sym}: {e}")
            else:
                log.warning(f"影子账户委员会评估未返回或失败，跳过影子交易执行: {sym}")
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

    # 3. 组合级提醒优化：现金预算 + 交易约束 + LLM/点位质量
    trading_mode = normalize_trading_mode(config.get("trading_mode", DEFAULT_TRADING_MODE))
    actionable, suppressed_alerts = select_optimal_actionable_alerts(
        results=results,
        prices=prices,
        cash=cash,
        stocks=all_stocks,
        entry_exit_state=entry_exit_state_before_round,
        max_alerts=int(config.get("max_action_alerts", 4) or 4),
        portfolio_value=total_assets,
        max_single_position_pct=float(config.get("max_single_position_pct", 25.0) or 25.0),
        max_sector_position_pct=float(config.get("max_sector_position_pct", 35.0) or 35.0),
        trading_mode=trading_mode,
    )

    real_holding_symbols = {
        str(h.get("symbol", "")).upper()
        for h in holdings
        if h.get("symbol") and (_safe_num(h.get("position_pct")) > 0 or _safe_num(h.get("units")) > 0)
    }

    entry_exit_alerts, entry_exit_watch = update_entry_exit_alert_state(
        results=results,
        prices=prices,
        holding_symbols=real_holding_symbols,
        stocks=all_stocks,
    )

    window_snapshot = build_monitor_window_snapshot(
        round_time=round_time,
        results=results,
        actionable=actionable,
        prices=prices,
        stocks=all_stocks,
        entry_exit_watch=entry_exit_watch,
        entry_exit_alerts=entry_exit_alerts,
        suppressed_alerts=suppressed_alerts,
        cash=cash,
        total_assets=total_assets,
        t2_pending_cash=t2_pending_cash,
        trading_mode=trading_mode,
    )
    write_monitor_window_snapshot(window_snapshot)

    # 4. 写报告
    write_report(
        round_time,
        results,
        actionable,
        prices,
        news_items,
        entry_exit_watch=entry_exit_watch,
        entry_exit_alerts=entry_exit_alerts,
        suppressed_alerts=suppressed_alerts,
    )
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

    # 5. 通知
    if not MONITOR_POPUPS_ENABLED:
        log.info("稳定监控窗口模式：跳过弹框。设置 INVEST_MONITOR_POPUPS=1 可恢复弹框。")
        if actionable:
            log.info(f"需要操作 {len(actionable)} 只标的，已写入 {LATEST_WINDOW_PATH}")
        return

    # - 整点/半点/尾盘：允许综合摘要（可含新闻）
    # - 其他交易时间：只有真正需要操作时才弹综合摘要；新闻刷新本身不弹窗
    # - 买卖点连续触发：始终单独弹窗
    import re as _re
    scheduled_summary = is_scheduled_monitor_popup_time(round_dt)
    if should_send_monitor_summary_popup(now=round_dt, actionable=actionable):
        toast_lines = []

        # 新闻源摘要只在整点/半点/尾盘综合摘要中展示，交易日新闻刷新本身不触发弹窗。
        if scheduled_summary and news_items:
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
                # 入场出场点
                ee = a.get("entry_exit_points", {}) or {}
                buy = ee.get("buy_pullback_price") or ee.get("buy_breakout_price")
                sl_p = ee.get("stop_loss_price")
                tp_p = ee.get("take_profit_price")
                exit_p = ee.get("trim_price")
                rr = ee.get("reward_risk_ratio", 0)

                # LLM审核
                review = a.get("optimizer_review", "") or ""
                rl = _re.search(r"ONE_LINE:\s*(.+)", review) if _re else None
                comment = rl.group(1)[:60] if rl else ""

                # 手数（买卖都按最小交易单位估算）
                alloc = a.get("suggested_alloc_cny", 0)
                sym = a.get("symbol", "")
                price = prices.get(sym, {}).get("price", 1)
                stock_cfg = next((s for s in all_stocks if s.get("symbol") == sym), {})
                lot = int(stock_cfg.get("min_lot_size") or 100)
                hands = int(abs(alloc) / (price * lot)) if price > 0 and abs(alloc) > 0 and lot > 0 else 0
                hands_str = f" {hands}手" if hands > 0 else ""

                current_shares = 0
                if price > 0:
                    current_shares = int(
                        stock_cfg.get("position_pct", 0) / 100 * config["total_assets"] / price
                    )
                current_hands = int(current_shares / lot) if lot > 0 else 0
                remain_hands = max(0, current_hands - hands) if alloc < 0 else current_hands
                remain_str = f" ->剩{remain_hands}手" if alloc < 0 and current_hands > 0 else ""

                v_map = {"ACCUMULATE": "🟢↑买", "BUY": "🟢↑买", "TRIM": "🔴↓卖", "SELL": "🔴↓卖"}
                v_icon = v_map.get(a['verdict'], a['verdict'])

                parts = [f"  {v_icon} {a['name']}{hands_str}{remain_str} ¥{alloc:,.0f}"]
                if buy: parts.append(f"买¥{buy:.2f}")
                if sl_p: parts.append(f"损¥{sl_p:.2f}")
                if tp_p: parts.append(f"盈¥{tp_p:.2f}")
                if exit_p: parts.append(f"出¥{exit_p:.2f}")
                if rr: parts.append(f"R{rr:.1f}")
                parts.append(f"(c={a['confidence']:.2f})")
                toast_lines.append(" ".join(parts))
                if comment:
                    toast_lines.append(f"    💬 {comment}")
        else:
            toast_lines.append("── ✅ 无需操作 ──")

        toast_body = "\n".join(toast_lines[:25])  # 最多25行
        send_windows_toast(
            f"📊 {round_time} 监控 ({len(actionable)}只需操作)",
            toast_body
        )
    else:
        log.info("跳过综合弹窗：非整点/半点/尾盘，且无交易操作提醒")
    if entry_exit_alerts:
        send_windows_toast(
            f"🚨 买卖点连续触发 ({len(entry_exit_alerts)}只)",
            format_entry_exit_alert_body(entry_exit_alerts),
        )
        log.info(f"🚨 买卖点连续触发: {len(entry_exit_alerts)} 只标的")
    if actionable:
        log.info(f"⚠️ 需要操作: {len(actionable)} 只标的")
    else:
        log.info("✅ 本轮无需操作")


def main():
    """主入口：循环执行监控"""
    log.info("=== OpenInvest 盘中监控启动 ===")
    log.info(f"交易时间: {TRADING_START}-{LUNCH_START}, {LUNCH_END}-{TRADING_END}, 间隔: {INTERVAL_MINUTES}分钟")
    log.info("委员会分析: 直接 Python 调用（无需后端端口）")

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

            # 交易时段：等待到下一个监控间隔节点
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
