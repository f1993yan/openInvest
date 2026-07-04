"""Monitor-window snapshot and markdown report writers."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jobs.market_monitor_common import LATEST_WINDOW_PATH, REPORT_DIR, log, _fmt_price, _safe_num
from jobs.market_monitor_alerts import _has_llm_hold_conflict, _suppressed_reasons_by_symbol
from jobs.trading_mode import DEFAULT_TRADING_MODE, trading_mode_payload


def _suppressed_details_by_symbol(suppressed_alerts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in suppressed_alerts:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        if item.get("discipline_review") and symbol not in out:
            out[symbol] = item
    return out

def _first_prefixed_line(text: str, prefixes: Tuple[str, ...]) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        for prefix in prefixes:
            if upper.startswith(prefix):
                return stripped.split(":", 1)[1].strip() if ":" in stripped else stripped
    return ""


def _operation_detail(
    result: Dict[str, Any],
    row: Dict[str, Any],
    actionable: Dict[str, Dict[str, Any]],
    suppressed_reasons: Optional[Any] = None,
) -> Dict[str, Any]:
    symbol = str(result.get("symbol") or row.get("symbol") or "").upper()
    action_row = actionable.get(symbol)
    suppressed_detail = None
    if suppressed_reasons and isinstance(suppressed_reasons, dict):
        suppressed_detail = suppressed_reasons
        suppressed_reason_labels = suppressed_detail.get("labels") or []
    else:
        suppressed_reason_labels = suppressed_reasons or []
    verdict = str((action_row or result).get("verdict") or "UNKNOWN").upper()
    committee_verdict = str(
        (action_row or {}).get("committee_verdict")
        or (action_row or {}).get("discipline_review", {}).get("committee_verdict")
        or result.get("verdict")
        or "UNKNOWN"
    ).upper()
    alloc_source = action_row if action_row else result
    alloc = _safe_num(alloc_source.get("suggested_alloc_cny"))
    triggers = row.get("triggers") or []
    if action_row:
        status = "action_required"
        reason = "selected_by_position_exit_discipline" if action_row.get("discipline_review") else "selected_by_cash_risk_optimizer"
    elif row.get("confirmed"):
        status = "trigger_confirmed"
        reason = "entry_exit_price_confirmed_two_rounds"
    elif triggers:
        status = "watch_trigger"
        reason = "price_touched_one_round_trigger"
    elif verdict in {"TRIM", "SELL"} and abs(alloc) > 0 and not triggers:
        status = "candidate"
        reason = (suppressed_reason_labels or ["committee_sell_waiting_for_exit_trigger"])[0]
    elif verdict in {"BUY", "ACCUMULATE", "TRIM", "SELL"} and abs(alloc) > 0:
        status = "candidate"
        reason = "committee_has_direction_but_not_selected"
    elif result.get("execution_blocked"):
        status = "blocked"
        reason = str(result.get("execution_block_reason") or "execution_guard")
    elif result.get("success"):
        status = "monitoring"
        reason = "no_action_now"
    else:
        status = "error"
        reason = str(result.get("error") or "analysis_failed")
    discipline_review = {}
    if action_row:
        discipline_review = action_row.get("discipline_review") or {}
    elif suppressed_detail:
        discipline_review = suppressed_detail.get("discipline_review") or {}
    return {
        "status": status,
        "reason": reason,
        "verdict": verdict,
        "committee_verdict": committee_verdict,
        "confidence": round(_safe_num(result.get("confidence")), 4),
        "suggested_alloc_cny": round(alloc, 2),
        "optimizer_lots": None if not action_row else int(_safe_num(action_row.get("optimizer_lots") or action_row.get("alert_selected_lots"))),
        "llm_review_lots": None if not action_row else int(_safe_num(action_row.get("llm_review_lots"), _safe_num(action_row.get("alert_selected_lots")))),
        "llm_position_scale": "" if not action_row else str(action_row.get("llm_position_scale") or ""),
        "llm_position_scale_value": None if not action_row else round(_safe_num(action_row.get("llm_position_scale_value")), 4),
        "llm_position_scale_multiplier": None if not action_row else round(_safe_num(action_row.get("llm_position_scale_multiplier"), 1.0), 4),
        "llm_risk_components": {} if not action_row else action_row.get("llm_risk_components") or {},
        "alert_score": None if not action_row else round(_safe_num(action_row.get("alert_score")), 2),
        "triggers": triggers,
        "alert_source": "" if not action_row else str(action_row.get("alert_source") or ""),
        "discipline_review": discipline_review,
        "confirmed": bool(row.get("confirmed")),
        "llm_conflict": _has_llm_hold_conflict(result),
        "execution_blocked": bool(result.get("execution_blocked")),
        "wait_reasons": suppressed_reason_labels,
    }


def _llm_operation_review(result: Dict[str, Any]) -> Dict[str, str]:
    review = result.get("optimizer_review") or ""
    memo = result.get("cio_memo") or ""
    return {
        "conclusion": _first_prefixed_line(review, ("CONCLUSION:",)),
        "one_line": _first_prefixed_line(review, ("ONE_LINE:",)),
        "risk_note": _first_prefixed_line(memo, ("RISK_PLAN:", "RISK:",)),
        "execution_plan": _first_prefixed_line(memo, ("EXECUTION_PLAN:",)),
        "raw_excerpt": (review or memo)[:1200],
    }


def build_monitor_window_snapshot(
    *,
    round_time: str,
    results: List[Dict[str, Any]],
    actionable: List[Dict[str, Any]],
    prices: Dict[str, Dict[str, Any]],
    stocks: List[Dict[str, Any]],
    entry_exit_watch: List[Dict[str, Any]],
    entry_exit_alerts: List[Dict[str, Any]],
    suppressed_alerts: List[Dict[str, Any]],
    cash: float,
    total_assets: float,
    t2_pending_cash: float = 0.0,
    trading_mode: str = DEFAULT_TRADING_MODE,
    monitor_action_email_enabled: bool = True,
) -> Dict[str, Any]:
    """Build the stable monitor-window payload consumed by the desktop UI."""
    result_by_symbol = {str(r.get("symbol") or "").upper(): r for r in results if r}
    stock_by_symbol = {str(s.get("symbol") or "").upper(): s for s in stocks if s.get("symbol")}
    price_by_symbol = {str(k).upper(): v for k, v in prices.items()}
    watch_by_symbol = {str(row.get("symbol") or "").upper(): row for row in entry_exit_watch}
    actionable_by_symbol = {str(row.get("symbol") or "").upper(): row for row in actionable}
    suppressed_by_symbol = _suppressed_reasons_by_symbol(suppressed_alerts)
    suppressed_details = _suppressed_details_by_symbol(suppressed_alerts)

    symbols = sorted(set(stock_by_symbol) | set(result_by_symbol) | set(price_by_symbol) | set(watch_by_symbol))
    rows: List[Dict[str, Any]] = []
    for symbol in symbols:
        stock = stock_by_symbol.get(symbol, {})
        result = result_by_symbol.get(symbol, {"success": False, "symbol": symbol, "name": stock.get("name", symbol)})
        price_info = price_by_symbol.get(symbol, {})
        row = watch_by_symbol.get(symbol, {})
        ee = result.get("entry_exit_points") or row.get("entry_exit_points") or {}
        position_exit_plan = row.get("position_exit_plan") or (
            (result.get("position_exit_plan") or {}) if isinstance(result, dict) else {}
        )
        units = _safe_num(stock.get("units"))
        pos_pct = _safe_num(stock.get("position_pct"))
        cost = _safe_num(stock.get("cost"))
        if units <= 0.0 and pos_pct > 0.0 and cost > 0.0 and total_assets > 0.0:
            units = (total_assets * pos_pct / 100.0) / cost
        elif pos_pct <= 0.0 and units > 0.0 and cost > 0.0 and total_assets > 0.0:
            pos_pct = (units * cost) / total_assets * 100.0

        is_holding = pos_pct > 0 or units > 0
        if is_holding and position_exit_plan:
            exit_points = {
                "stop_loss_price": _safe_num(
                    position_exit_plan.get("effective_stop_price"),
                    _safe_num(position_exit_plan.get("hard_stop_price")),
                ),
                "take_profit_price": _safe_num(position_exit_plan.get("take_profit_1_price")),
                "take_profit_2_price": _safe_num(position_exit_plan.get("take_profit_2_price")),
                "trim_price": _safe_num(position_exit_plan.get("trim_price")),
                "trailing_stop_price": _safe_num(position_exit_plan.get("trailing_stop_price")),
                "hard_stop_price": _safe_num(position_exit_plan.get("hard_stop_price")),
                "plan_type": position_exit_plan.get("plan_type", "a_share_position_exit"),
                "locked_intraday": bool(position_exit_plan.get("locked_intraday", True)),
                "created_at": position_exit_plan.get("created_at", ""),
                "last_updated_after_close": position_exit_plan.get("last_updated_after_close", ""),
                "reason": position_exit_plan.get("reason", ""),
            }
        else:
            exit_points = {
                "stop_loss_price": _safe_num(ee.get("stop_loss_price")),
                "take_profit_price": _safe_num(ee.get("take_profit_price")),
                "trim_price": _safe_num(ee.get("trim_price")),
                "plan_type": "pre_trade_estimate",
                "locked_intraday": False,
            }
        suppressed_context: Any = suppressed_by_symbol.get(symbol, [])
        if symbol in suppressed_details:
            suppressed_context = {
                **suppressed_details[symbol],
                "labels": suppressed_by_symbol.get(symbol, []),
            }
        operation = _operation_detail(result, row, actionable_by_symbol, suppressed_context)
        rows.append({
            "symbol": symbol,
            "name": result.get("name") or stock.get("name") or price_info.get("name") or symbol,
            "market": result.get("market") or stock.get("market", "a"),
            "sector": stock.get("sector", ""),
            "industry": stock.get("industry", ""),
            "min_lot_size": int(_safe_num(stock.get("min_lot_size"), 100) or 100),
            "units": round(units, 4),
            "is_holding": is_holding,
            "position_pct": round(pos_pct, 4),
            "target_position_pct": stock.get("target_position_pct", stock.get("target_pct")),
            "cost": _safe_num(stock.get("cost")),
            "entry_exit_points": ee,
            "position_exit_plan": position_exit_plan,
            "right_side_trend_gate": result.get("right_side_trend_gate", {}),
            "price": {
                "current": _safe_num(price_info.get("price"), _safe_num(ee.get("current_price"))),
                "prev_close": _safe_num(price_info.get("prev_close")),
                "change_pct": _safe_num(price_info.get("change_pct")),
                "name": price_info.get("name", ""),
            },
            "state": operation["status"],
            "buy_criteria": {
                "pullback_price": _safe_num(ee.get("buy_pullback_price")),
                "breakout_price": _safe_num(ee.get("buy_breakout_price")),
                "reentry_price": _safe_num(ee.get("reentry_price")),
                "reward_risk_ratio": _safe_num(ee.get("reward_risk_ratio")),
                "reason": ee.get("reason", ""),
            },
            "exit_points": exit_points,
            "fundamental": {
                "model": result.get("fundamental_model", ""),
                "score": _safe_num(result.get("fundamental_score"), 50.0),
                "coverage": _safe_num(result.get("fundamental_coverage")),
                "anchor_multiplier": _safe_num(result.get("fundamental_anchor_multiplier"), 1.0),
            },
            "technical": {
                "regime": result.get("regime", ""),
                "quant_view": result.get("quant_view", ""),
                "market_data_excerpt": result.get("market_data", "")[:1200],
                "entry_exit_model": ee.get("model_name", ""),
                "low_confidence": bool(ee.get("low_confidence")),
                "atr_pct": _safe_num(ee.get("atr_pct")),
                "expected_return_pct": _safe_num(ee.get("expected_return_pct")),
                "buy_signal_backtest": result.get("buy_signal_backtest", {}),
            },
            "operation": operation,
            "buy_signal_backtest": result.get("buy_signal_backtest", {}),
            "decision_synthesis": result.get("decision_synthesis", {}),
            "llm_review": _llm_operation_review(result),
            "suppressed_reasons": suppressed_by_symbol.get(symbol, []),
            "error": result.get("error", ""),
            "success": bool(result.get("success")),
        })

    available_cash = max(_safe_num(cash), 0.0)
    pending_cash = max(_safe_num(t2_pending_cash), 0.0)
    total_cash = available_cash + pending_cash
    return {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "round_time": round_time,
        "trading_mode": trading_mode_payload(trading_mode),
        "monitor_action_email_enabled": bool(monitor_action_email_enabled),
        "cash_cny": round(available_cash, 2),
        "available_cash_cny": round(available_cash, 2),
        "t2_pending_cash_cny": round(pending_cash, 2),
        "total_cash_cny": round(total_cash, 2),
        "total_assets_cny": round(_safe_num(total_assets), 2),
        "counts": {
            "symbols": len(rows),
            "action_required": sum(1 for row in rows if row["state"] == "action_required"),
            "entry_exit_alerts": len(entry_exit_alerts),
            "suppressed": len(suppressed_alerts),
            "errors": sum(1 for row in rows if not row["success"]),
        },
        "rows": rows,
        "actionable": actionable,
        "entry_exit_alerts": entry_exit_alerts,
        "suppressed_alerts": suppressed_alerts,
    }


def write_monitor_window_snapshot(snapshot: Dict[str, Any], path: Path = LATEST_WINDOW_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_report(round_time: str, results: List[Dict],
                 actionable: List[Dict], prices: Dict,
                 news_items: List = None,
                 entry_exit_watch: Optional[List[Dict[str, Any]]] = None,
                 entry_exit_alerts: Optional[List[Dict[str, Any]]] = None,
                 suppressed_alerts: Optional[List[Dict[str, Any]]] = None):
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
        "| 股票 | Verdict | 置信度 | 建议 | Fundamental | OptReview | Quant | Regime |",
        "|------|---------|--------|------|-------------|-----------|-------|--------|",
    ])
    for r in results:
        if r.get("success"):
            fundamental = r.get("fundamental_model", "")
            if r.get("fundamental_score") is not None:
                fundamental = f"{fundamental}:{r.get('fundamental_score', 50):.0f}"
            review = ""
            for line in (r.get("optimizer_review") or "").splitlines():
                if line.startswith("CONCLUSION:"):
                    review = line.split(":", 1)[1].strip()
                    break
            lines.append(
                f"| {r['name']} | **{r['verdict']}** | {r['confidence']:.2f} | "
                f"¥{r.get('suggested_alloc_cny', 0):,.0f} | "
                f"{fundamental} | "
                f"{review or '-'} | "
                f"{r.get('quant_view', '')[:80]}... | "
                f"{r.get('regime', '')} |"
            )
        else:
            lines.append(f"| {r.get('name', r.get('symbol'))} | ❌ 失败 | — | — | — | — | — | — |")

    guarded = [r for r in results if r.get("execution_blocked")]
    if guarded:
        lines.extend([
            "",
            "## 🧯 执行保护",
            "",
        ])
        for r in guarded:
            lines.append(
                f"- {r.get('name')} ({r.get('symbol')}): "
                f"{r.get('optimizer_verdict_before_guard')} "
                f"¥{_safe_num(r.get('optimizer_alloc_cny_before_guard')):,.0f} -> HOLD；"
                f"{r.get('execution_block_reason')}"
            )

    if suppressed_alerts:
        lines.extend([
            "",
            "## 🧮 提醒优化过滤",
            "",
        ])
        for item in suppressed_alerts[:20]:
            lines.append(
                f"- {item.get('name') or item.get('symbol')}: {item.get('reason')}"
            )

    if entry_exit_watch:
        lines.extend([
            "",
            "## 🎯 买入卖出点监控",
            "",
            "| 股票 | 类型 | 现价 | 买回调 | 买突破 | 止损 | 止盈 | 减仓 | 触发状态 |",
            "|------|------|------|--------|--------|------|------|------|----------|",
        ])
        for row in entry_exit_watch:
            ee = row.get("entry_exit_points") or {}
            trigger_text = "、".join(
                f"{t.get('side')}:{t.get('kind')}@{_fmt_price(t.get('level'))}"
                for t in (row.get("triggers") or [])[:3]
            )
            if row.get("confirmed"):
                trigger_text = f"连续触发({trigger_text})"
            lines.append(
                f"| {row.get('name')} ({row.get('symbol')}) | "
                f"{'持仓' if row.get('is_holding') else '关注'} | "
                f"{_fmt_price(row.get('current_price'))} | "
                f"{_fmt_price(ee.get('buy_pullback_price'))} | "
                f"{_fmt_price(ee.get('buy_breakout_price'))} | "
                f"{_fmt_price(ee.get('stop_loss_price'))} | "
                f"{_fmt_price(ee.get('take_profit_price'))} | "
                f"{_fmt_price(ee.get('trim_price'))} | "
                f"{trigger_text or '-'} |"
            )

    if entry_exit_alerts:
        lines.extend([
            "",
            "## 🚨 连续触发提醒",
            "",
        ])
        for alert in entry_exit_alerts:
            lines.append(
                f"- {alert.get('name')} ({alert.get('symbol')}) "
                f"现价 {_fmt_price(alert.get('current_price'))} "
                f"方向 {','.join(alert.get('matched_sides') or [])}"
            )

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
