import os
import re
import sys
import json
import logging
import _pickle
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

log = logging.getLogger("openinvest.phone_committee")

CACHE_DIR = _PROJECT_ROOT / "data" / "committee_cache"

def _safe_num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def _fmt_price(value: Any) -> str:
    val = _safe_num(value)
    return f"{val:.2f}" if val > 0 else "-"

def _fmt_pct(value: Any) -> str:
    return f"{_safe_num(value):+.2f}%"

def _fmt_money(value: Any) -> str:
    val = _safe_num(value)
    if val == 0:
        return "0"
    return f"{val:,.0f}" if abs(val) >= 1 else "-"

def _verdict_label(verdict: Any) -> str:
    return {
        "BUY": "买入",
        "ACCUMULATE": "小幅加仓",
        "HOLD": "持有不动",
        "WAIT": "等待",
        "TRIM": "减仓",
        "SELL": "卖出",
    }.get(str(verdict or "").upper(), "暂无明确结论")

def _regime_label(text: Any) -> str:
    value = str(text or "")
    match = re.search(r"REGIME:\s*([a-z_]+)", value, re.I)
    regime = match.group(1).lower() if match else ""
    label = {
        "uptrend": "上升趋势",
        "downtrend": "下跌趋势",
        "range_bound": "震荡",
        "crash": "急跌",
        "recovery": "修复",
    }.get(regime, "趋势不明")
    reason_match = re.search(r"REASON:\s*([^\n]+)", value)
    reason = reason_match.group(1).strip() if reason_match else ""
    return f"{label}: {reason}" if reason else label

def _clean_cio_memo_for_brief(text: str) -> str:
    lines = str(text or "").splitlines()
    clean_lines = []
    for line in lines:
        t = line.strip()
        if not t:
            continue
        if re.match(r"^[A-Z0-9_]+:\s*", t):
            continue
        if t.startswith("==") or t.startswith("##") or t.startswith("-"):
            continue
        clean_lines.append(t)
    return " ".join(clean_lines)

def _extract_one_line(text: Any) -> str:
    value = str(text or "").strip()
    for key in ("ONE_LINE:", "HUMAN_CHECK:", "RATIONALE:"):
        match = re.search(rf"{key}\s*(.+?)(?=\s+[A-Z_]+:|$)", value, re.S)
        if match:
            return _short(match.group(1).strip(), 140)
    cleaned = _clean_cio_memo_for_brief(value)
    return _short(cleaned, 140) if cleaned else "-"

def _short(text: Any, limit: int = 100) -> str:
    value = str(text or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "..."

def _extract_risk_flags(text: Any) -> List[str]:
    value = str(text or "")
    match = re.search(r"RISK_FLAGS:\s*(.+?)(?=\s+[A-Z_]+:|$)", value, re.S)
    if not match:
        return []
    return [
        item.strip(" ，,;；")
        for item in re.split(r"[,，;；]", match.group(1))
        if item.strip(" ，,;；")
    ][:3]

def _distance_pct(current: Any, target: Any) -> Optional[float]:
    current_num = _safe_num(current)
    target_num = _safe_num(target)
    if current_num <= 0 or target_num <= 0:
        return None
    return (target_num - current_num) / current_num * 100.0

def _fmt_distance(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if abs(value) < 0.05:
        return "几乎就在当前价"
    direction = "上方" if value > 0 else "下方"
    return f"{abs(value):.1f}% {direction}"

def _beginner_next_step(verdict: Any, current: Any, pullback: Any, breakout: Any, stop: Any) -> str:
    verdict_key = str(verdict or "").upper()
    current_num = _safe_num(current)
    pullback_num = _safe_num(pullback)
    breakout_num = _safe_num(breakout)
    stop_num = _safe_num(stop)

    if verdict_key in {"BUY", "ACCUMULATE"}:
        if pullback_num > 0 and current_num <= pullback_num:
            return "当前价已低于回调买点，符合低吸要求，可分批买入。"
        if breakout_num > 0 and current_num >= breakout_num:
            return "价格突破阻力位，可考虑底仓跟进。"
        return "等待价格回到理想买入区间，勿盲目追高。"
    if verdict_key in {"SELL", "TRIM"}:
        if stop_num > 0 and current_num <= stop_num:
            return "当前价已跌破止损线，应坚决减仓或离场避险。"
        return "根据出场纪律，建议逢高减持，降低持仓风险。"
    return "保持观望，等待下一次明确的委员会买卖建议。"

def build_mobile_recommendation_text(
    symbol: str,
    name: str,
    market: str,
    current_price: float,
    change_pct: float,
    verdict: str,
    confidence: float,
    suggested_alloc_cny: float,
    is_holding: bool,
    entry_exit_points: Dict[str, Any],
    position_exit_policy: Dict[str, Any],
    right_side_trend_gate: Dict[str, Any],
    fundamental_score: float,
    regime_brief: str,
    cio_memo: str,
    is_from_cache: bool = False,
    decision_synthesis: Optional[Dict[str, Any]] = None,
    buy_signal_backtest: Optional[Dict[str, Any]] = None,
    behavioral_factor: Optional[Dict[str, Any]] = None,
) -> str:
    current = _safe_num(current_price)
    confidence_val = _safe_num(confidence)
    alloc = _safe_num(suggested_alloc_cny)

    pullback = _safe_num(entry_exit_points.get("buy_pullback_price"))
    breakout = _safe_num(entry_exit_points.get("buy_breakout_price"))

    stop = _safe_num(position_exit_policy.get("stop_loss_price"))
    if not stop or stop <= 0:
        stop = _safe_num(entry_exit_points.get("stop_loss_price"))

    take = _safe_num(position_exit_policy.get("take_profit_price"))
    if not take or take <= 0:
        take = _safe_num(entry_exit_points.get("take_profit_price"))

    plan_type = str(position_exit_policy.get("plan_type") or "")

    pullback_dist = _distance_pct(current, pullback)
    breakout_dist = _distance_pct(current, breakout)
    stop_dist = _distance_pct(current, stop)
    take_dist = _distance_pct(current, take)

    action = _verdict_label(verdict)
    if decision_synthesis:
        action = str(decision_synthesis.get("action_label") or action)
        primary_reason = str(decision_synthesis.get("primary_reason") or "")
        decision = f"{action}，{primary_reason}" if primary_reason else f"{action}。"
    elif str(verdict or "").upper() in {"BUY", "ACCUMULATE"} and current > 0 and _safe_num(pullback) > 0 and current > _safe_num(pullback) * 1.03:
        decision = f"{action}，但当前价离回调买点偏高，别急着追。"
    elif str(verdict or "").upper() in {"BUY", "ACCUMULATE"}:
        decision = f"{action}，先看是否接近买点。"
    elif str(verdict or "").upper() in {"SELL", "TRIM"}:
        decision = f"{action}，注意减仓离场防范风险。"
    else:
        decision = f"{action}。"

    # Text formatting
    lines = [
        "你最该先看这儿呗:",
        f"1. 当前价: {_fmt_price(current)}，今日涨跌 {_fmt_pct(change_pct)}。",
        f"2. 决策结论: {decision}",
        f"3. 理想买点: 回调 {_fmt_price(pullback)} (在当前价 {_fmt_distance(pullback_dist)})；突破 {_fmt_price(breakout)} (在当前价 {_fmt_distance(breakout_dist)})。",
        f"4. 风险线: {'持仓纪律' if plan_type == 'a_share_position_exit' else '入场估算'}，止损 {_fmt_price(stop)} (在当前价 {_fmt_distance(stop_dist)})；止盈 {_fmt_price(take)} (在当前价 {_fmt_distance(take_dist)})。",
        f"5. 仓位建议: {_fmt_money(alloc)} 元；置信度 {int(confidence_val * 100)}%；当前{'已有持仓' if is_holding else '没有持仓'}。",
        "",
        "为什么这么判断:",
        f"- 技术面: {_regime_label(regime_brief)}",
        f"- 右侧趋势闸门: {'通过' if right_side_trend_gate.get('allow') else '未通过'} ({right_side_trend_gate.get('reason') or '未提供'}) 。",
        f"- 基本面: {fundamental_score:.0f} 分，属于{'偏强' if fundamental_score >= 70 else '一般' if fundamental_score >= 45 else '偏弱'}。"
    ]

    if behavioral_factor:
        factor_score = _safe_num(behavioral_factor.get("score"))
        target_weight = _safe_num(behavioral_factor.get("target_weight_pct"))
        optimizer_weight = _safe_num(behavioral_factor.get("optimizer_weight"))
        trailing_return = _safe_num(behavioral_factor.get("trailing_3m_factor_return_pct"))
        hit_rate = _safe_num(behavioral_factor.get("trailing_3m_hit_rate"))
        sample_size = int(_safe_num(behavioral_factor.get("trailing_3m_sample_size")))
        selected_text = "入选前四" if behavioral_factor.get("selected") else "未进入前四"
        confidence_text = "低置信度" if behavioral_factor.get("low_confidence") else "有效"
        lines.extend([
            f"- A股行为因子: {factor_score:.1f}分，{selected_text}，目标仓位 {target_weight:.1f}%（{confidence_text}）。",
            f"- 因子历史: 近3月 {trailing_return:+.1f}%，命中 {hit_rate * 100:.0f}% (n={sample_size})，优化权重 {optimizer_weight:.2f}。",
        ])

    if decision_synthesis:
        for item in (decision_synthesis.get("evidence") or [])[:3]:
            lines.append(f"- 决策证据: {item}")
        for item in (decision_synthesis.get("conflicts") or [])[:2]:
            lines.append(f"- 口径冲突: {item}；最终按确定性优化器执行。")

    if buy_signal_backtest:
        from core.buy_signal_miner import buy_signal_summary_text
        lines.append(f"- {buy_signal_summary_text(buy_signal_backtest)}")

    one_line = _extract_one_line(cio_memo)
    if is_from_cache:
        lines.append("- 模型提醒: 从本地缓存载入上一次委员会分析结果")
    elif one_line and one_line != "-":
        lines.append(f"- 模型提醒: {one_line}")

    risk_flags = _extract_risk_flags(cio_memo)
    low_confidence = bool(entry_exit_points.get("low_confidence"))

    synthesis_warnings = list((decision_synthesis.get("risk_warnings") or [])[:3]) if decision_synthesis else []
    signal_warning = str((buy_signal_backtest or {}).get("warning") or "") if buy_signal_backtest else ""

    if risk_flags or low_confidence or synthesis_warnings or signal_warning:
        lines.extend(["", "需要小心:"])
        if low_confidence:
            lines.append("- 买卖点模型置信度偏低，价格线只能当参考，不能机械下单。")
        if plan_type == "a_share_position_exit":
            lines.append("- 已持仓标的的止盈止损盘中不重算，只在收盘后按追踪规则上移风险线。")
        for warning in synthesis_warnings:
            lines.append(f"- {warning}")
        if signal_warning:
            lines.append(f"- {signal_warning}")
        for flag in risk_flags:
            lines.append(f"- {flag}")

    lines.extend([
        "",
        "下一步:",
        _beginner_next_step(verdict, current, pullback, breakout, stop)
    ])

    return "\n".join(lines)

def _save_committee_cache(**kwargs):
    """二进制缓存委员会分析结果"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        cache_dir = CACHE_DIR / today
        cache_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H%M")
        cache_file = cache_dir / f"{ts}_{kwargs['symbol']}.pkl"
        kwargs["cached_at"] = datetime.now().isoformat()
        with open(cache_file, "wb") as f:
            _pickle.dump(kwargs, f)
    except Exception as e:
        log.warning(f"缓存写入失败: {e}")

def run_committee_local(
    symbol: str,
    name: str,
    market: str,
    sector: str,
    industry: str,
    position_pct: float,
    target_position_pct: Optional[float],
    cost: float,
    current_price: Optional[float],
    total_assets: float,
    cash: float,
    llm_api_key: str,
    llm_model: str,
    server_ip: str,
    holdings_json: str,
    llm_base_url: str = "",
    news_brief: str = "",
    min_lot_size: int = 100,
    t_plus_1: bool = True,
    available_cash: float = 0.0,
    t2_pending_cash: float = 0.0,
    optimizer_review_enabled: bool = True,
    max_debate_rounds: int = 4,
    server_port: str = "8765",
    change_pct: float = 0.0,
    trading_mode: str = "active_profit",
    ma20: Optional[float] = None,
    ma120: Optional[float] = None,
    atr_pct: Optional[float] = None
) -> str:
    """本地手机端执行投资委员会分析的入口"""
    t0 = datetime.now()

    # Set up environment variables for the phone run
    os.environ["LLM_API_KEY"] = llm_api_key
    os.environ["LLM_MODEL"] = llm_model
    if llm_base_url:
        os.environ["LLM_BASE_URL"] = llm_base_url
    else:
        # Clear if was set previously
        os.environ.pop("LLM_BASE_URL", None)
    os.environ["INVEST_SERVER_IP"] = server_ip
    os.environ["INVEST_SERVER_PORT"] = server_port
    os.environ["INVEST_RUNNING_ON_PHONE"] = "1"
    os.environ["INVEST_TRADING_MODE"] = trading_mode

    # Configure LLM provider
    if "gemini" in llm_model.lower():
        os.environ["LLM_PROVIDER"] = "gemini"
    else:
        os.environ["LLM_PROVIDER"] = "deepseek"

    # Configure cache dir to a writable path on Android if possible
    # We can write under the current directory's parent (which is often /data/data/com.f1993yan.openInvest/files/...)
    global CACHE_DIR
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fallback to tmp directory on Android
        try:
            import tempfile
            CACHE_DIR = Path(tempfile.gettempdir()) / "committee_cache"
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    try:
        # Parse holdings
        holdings = []
        try:
            raw_holdings = json.loads(holdings_json) if holdings_json else []
            for h in raw_holdings:
                class TempHolding:
                    def __init__(self, **kwargs):
                        self.__dict__.update(kwargs)
                holdings.append(TempHolding(
                    symbol=h.get("symbol", ""),
                    name=h.get("name", ""),
                    weight_pct=float(h.get("weight_pct", 0.0)),
                    cost=float(h.get("cost", 0.0)),
                    current_price=h.get("current_price")
                ))
        except Exception as e:
            log.warning(f"Failed to parse holdings: {e}")

        # 0. 科创板 check
        if symbol.startswith("688") and position_pct == 0:
            min_lot_size = max(min_lot_size, 200)

        # 1. 查现价
        if current_price is None or current_price == 0.0:
            try:
                from utils.akshare_data import get_history_data
                df = get_history_data(symbol, "5d")
                if not df.empty:
                    current_price = float(df["Close"].iloc[-1])
            except Exception:
                current_price = 0.0

        # 2. 拉市场数据
        from utils.akshare_data import get_history_data, analyze_multi_timeframe, get_macro_data
        from utils.market_metrics import compute_metrics
        from core.regime import format_regime_brief

        df_2y = get_history_data(symbol, "2y")
        tech_report = ""
        if df_2y.empty:
            metrics = {}
        else:
            tech_report = analyze_multi_timeframe(df_2y, symbol)
            metrics = compute_metrics(df_2y)

        # Inject metrics passed from phone side (e.g. from server snapshot cache)
        if ma20 is not None:
            metrics["ma20"] = ma20
        if ma120 is not None:
            metrics["ma120"] = ma120
        if atr_pct is not None:
            metrics["atr_pct"] = atr_pct

        if metrics.get("ma20") is None or metrics.get("ma120") is None or metrics.get("atr_pct") is None:
            # Fallback to local resolved_snapshot.json cache if still missing
            try:
                from pathlib import Path
                snapshot_path = Path("/data/data/com.f1993yan.openInvest/files/resolved_snapshot.json")
                if snapshot_path.exists():
                    snap_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
                    for r in (snap_data.get("rows") or []):
                        if str(r.get("symbol") or "").strip() == symbol.strip():
                            tech = r.get("technical") or {}
                            for k, py_k in [("ma20", "ma20"), ("ma120", "ma120"), ("atr_pct", "atr_pct")]:
                                if metrics.get(py_k) is None and tech.get(k) is not None:
                                    metrics[py_k] = float(tech[k])
            except Exception as se:
                log.warning(f"Failed to read phone local resolved_snapshot.json fallback: {se}")

        if not metrics or metrics.get("ma20") is None:
            market_data = "市场数据暂缺（akshare 查询失败且本地快照无指标）"
            regime_brief = "无技术判定（数据为空）"
        else:
            regime_brief = format_regime_brief(metrics, symbol=symbol)
            macro_data = get_macro_data()
            tech_section = tech_report if tech_report else "(技术指标由快照/外部传入提供)"
            market_data = f"{macro_data}\n\n--- 技术分析 ---\n{tech_section}\n\n--- REGIME ---\n{regime_brief}"

        # 3. 拉 regime brief 及基本面 snapshot
        from core.fundamental_model import assess_fundamentals
        from utils.fundamental_data import get_fundamental_snapshot

        fundamental_snapshot = get_fundamental_snapshot(symbol, market)
        auto_fundamentals = fundamental_snapshot.get("metrics", {})
        fundamental_assessment = assess_fundamentals(
            symbol=symbol,
            name=name,
            sector=sector,
            industry=industry,
            market=market,
            metrics=auto_fundamentals,
        )
        fundamental_brief = fundamental_assessment.summary_text()
        fundamental_brief += f"\nDATA_SOURCE: remote metrics={len(auto_fundamentals)}"
        market_data += f"\n\n--- FUNDAMENTAL MODEL ---\n{fundamental_brief}"

        # 3.5 拉取 A 股行为因子评估
        behavioral_assessment = None
        if market.lower() == "a" and len(symbol) == 6:
            try:
                import requests
                url = f"http://{server_ip}:{server_port}/api/stock/behavioral?symbol={symbol}"
                resp = requests.get(url, timeout=10)
                if resp.status_code == 200:
                    res = resp.json()
                    if res.get("success") and res.get("assessment"):
                        from core.ashare_behavioral_factor import AShareBehavioralAssessment
                        behavioral_assessment = AShareBehavioralAssessment.from_mapping(res["assessment"])
            except Exception as e:
                log.warning(f"Phone fetch behavioral assessment error: {e}")

        if behavioral_assessment is not None:
            market_data += behavioral_assessment.audit_text()

        # 4. 获取宏观视图（含新闻）
        macro_data_str = get_macro_data()
        if news_brief:
            macro_data_str += f"\n\n## 当前新闻\n{news_brief}"
        from core.committee import run_macro_view
        macro_view = run_macro_view(macro_data_str)

        # 5. 构建输入
        # Replicate _build_portfolio_summary
        lines = [
            f"## 投资组合概览",
            f"- 总资产约: ¥{total_assets:,.0f}",
            f"- 可用现金: ¥{cash:,.0f} ({cash/total_assets*100:.1f}%)",
            f"- 持仓数: {len(holdings) + (1 if position_pct > 0 else 0)} 只",
            f"",
            f"### 当前持仓明细",
        ]
        lines.append(
            f"- **{name or symbol}** ({symbol}): "
            f"仓位 {position_pct:.1f}%, "
            f"成本 ¥{cost:.2f}, "
            f"现价 ¥{current_price or 'N/A'}, "
            f"集中度 {position_pct:.1f}%"
        )
        for h in holdings:
            lines.append(
                f"- {h.name} ({h.symbol}): "
                f"仓位 {h.weight_pct:.1f}%, "
                f"成本 ¥{h.cost:.2f}, "
                f"集中度 {h.weight_pct:.1f}%"
            )
        if news_brief:
            lines.append("")
            lines.append("### 📰 当前新闻摘要（影响宏观判断和CIO决策）")
            lines.append(news_brief)

        if min_lot_size > 0 or t_plus_1 or available_cash > 0:
            price = current_price or 0
            if price > 0 and min_lot_size > 0:
                per_hand = price * min_lot_size
                max_hands = int(available_cash / per_hand) if available_cash > 0 and per_hand > 0 else 0
            else:
                per_hand = 0
                max_hands = 0

            lines.append("")
            lines.append("### ⚠️ 交易约束（CIO必须严格遵守，违反则建议无效）")
            if per_hand > 0:
                lines.append(f"- 🔴 **1手 = {min_lot_size}股 ≈ ¥{per_hand:,.0f}**（= 当前价 ¥{price:.2f} × {min_lot_size}）")
                lines.append(f"- 🔴 **可用资金最多买 {max_hands} 手**（¥{available_cash:,.0f} ÷ ¥{per_hand:,.0f}）")
                lines.append(f"- 🔴 **suggested_alloc_cny 必须 = ¥{per_hand:,.0f} × N手**（N为整数，且N ≤ {max_hands}）")
                lines.append(f"- 🔴 **禁止出现金额不够1手的建议**（< ¥{per_hand:,.0f} 的建议直接无效）")
            if t_plus_1:
                lines.append("- 🔴 **T+1 交易规则**: A股当日买入次日方可卖出")
            if market == "hk":
                lines.append("- 🔴 **港股通T+2交收**: 卖出港股后资金T+2才到账，当日不可用于买入A股")
                if t2_pending_cash > 0:
                    lines.append(f"  ⚠️ 当前T+2待交收: ¥{t2_pending_cash:,.0f}（不可用）")
            if symbol.startswith("688") and position_pct == 0:
                lines.append("- 🔴 **科创板首次买入: 最低200股（2手）**，之后可100股递增")

        portfolio_summary = "\n".join(lines)
        portfolio_summary += f"\n\n### 基本面数学模型锚点\n{fundamental_brief}"

        from core.position_exit_policy import load_position_exit_policy
        position_exit_policy = load_position_exit_policy(sector=sector)
        position_exit_policy_text = position_exit_policy.audit_text(
            symbol=symbol,
            market=market,
            is_holding=position_pct > 0,
            cost=cost,
            current_price=current_price or 0.0,
        )
        portfolio_summary += f"\n\n### 已持仓A股止盈止损纪律（不要当作入场点）\n{position_exit_policy_text}"

        from core.committee import run_wealth_context_view
        wealth_context = run_wealth_context_view(None, cash)

        # 6. 跑委员会
        log.info(f"启动本地手机端委员会辩论...")
        from core.committee import run_committee, parse_cio_memo

        asset = {
            "symbol": symbol,
            "display_name": name or symbol,
        }

        result = run_committee(
            asset=asset,
            market_data=market_data,
            macro_view=macro_view,
            portfolio_summary=portfolio_summary,
            prior_insights="",
            regime_brief=regime_brief,
            wealth_context_view=wealth_context,
            current_price=current_price or None,
            persist_to_memory=False,
            max_debate_rounds=max_debate_rounds,
        )

        report = result.get("report")
        if report is None:
            return json.dumps({
                "success": False,
                "symbol": symbol,
                "name": name,
                "error": f"本地委员会返回空 report: {result.get('error', 'unknown')}",
                "elapsed_sec": (datetime.now() - t0).total_seconds(),
            }, ensure_ascii=False)

        cio_memo = report.cio_memo or ""
        from core.committee import parse_cio_memo as server_parse_cio_memo
        parsed = server_parse_cio_memo(cio_memo, current_price=current_price)

        atr_pct = metrics.get("atr_pct") if metrics else None
        if atr_pct and current_price and current_price > 0:
            atr_amount = current_price * atr_pct / 100
            atr_sl = current_price - 2 * atr_amount
            atr_tp = current_price + 3 * atr_amount
            cio_memo += (
                f"\n[ATR] ATR={atr_amount:.2f}({atr_pct:.1f}%) "
                f"止损2N={atr_sl:.2f} 止盈3N={atr_tp:.2f}"
            )

        regime_probability = None
        conditional_return_stats = None
        try:
            from core.regime_probability import (
                build_probability_table_from_ohlc,
                get_conditional_return_stats,
                get_regime_probability,
            )
            regime_label = ""
            for line in regime_brief.splitlines():
                if line.startswith("REGIME:"):
                    regime_label = line.split(":", 1)[1].strip()
                    break
            if regime_label:
                prob_table = build_probability_table_from_ohlc([symbol])
                regime_probability = get_regime_probability(
                    symbol.upper(), regime_label, table=prob_table,
                )
                conditional_return_stats = get_conditional_return_stats(
                    symbol.upper(), regime_label,
                )
        except Exception as e:
            log.warning(f"regime 概率表不可用: {e}")

        from core.decision_optimizer import optimize_committee_decision
        from core.entry_exit_points import compute_entry_exit_points

        opt = optimize_committee_decision(
            parsed=parsed,
            metrics=metrics,
            symbol=symbol,
            regime_brief=regime_brief,
            current_price=current_price,
            total_assets=total_assets,
            available_cash=available_cash if available_cash > 0 else cash,
            position_pct=position_pct,
            min_lot_size=min_lot_size,
            bl_anchor_target_pct=target_position_pct,
            market=market,
            risk_preference="moderate",
            regime_probability=regime_probability,
            conditional_return_stats=conditional_return_stats,
            fundamental_assessment=fundamental_assessment,
            position_exit_policy=position_exit_policy,
            behavioral_assessment=behavioral_assessment,
        )
        entry_exit_plan = compute_entry_exit_points(
            symbol=symbol,
            current_price=current_price,
            metrics=metrics,
            regime_brief=regime_brief,
            market=market,
            conditional_return_stats=conditional_return_stats,
            expected_return_pct=opt.expected_return_pct,
        )

        from core.right_side_trend_gate import evaluate_right_side_trend_gate
        right_side_gate = evaluate_right_side_trend_gate(
            metrics=metrics,
            regime_brief=regime_brief,
            optimizer_expected_return_pct=opt.expected_return_pct,
            entry_exit_points=entry_exit_plan.as_dict(),
            conditional_return_stats=conditional_return_stats,
            quant_view=report.quant_view or "",
            risk_view=report.risk_view or "",
            cio_memo=cio_memo,
            market=market,
            is_holding=position_pct > 0,
        )

        opt_audit_text = opt.audit_text()
        entry_exit_audit_text = entry_exit_plan.audit_text()
        right_side_gate_text = right_side_gate.audit_text()

        optimizer_review = ""
        if optimizer_review_enabled:
            try:
                from core.committee import run_optimizer_review_view
                optimizer_review = run_optimizer_review_view(
                    asset=asset,
                    optimizer_audit=opt_audit_text,
                    entry_exit_audit=entry_exit_audit_text,
                    right_side_gate_audit=right_side_gate_text,
                    position_exit_policy_audit=position_exit_policy_text,
                    regime_brief=regime_brief,
                    fundamental_brief=fundamental_brief,
                )
            except Exception as e:
                optimizer_review = f"[WORKER_UNAVAILABLE] reason=optimizer_review_failed exc_type={type(e).__name__}"

        has_loss = (
            position_pct > 0
            and cost > 0
            and current_price
            and current_price < cost
        )
        if has_loss and opt.verdict in ("SELL", "TRIM"):
            cio_memo += (
                f"\n[FLOATING_LOSS_PROTECT] 持仓浮亏({(current_price/cost-1)*100:.1f}%)，"
                f"优化器{opt.verdict}→保留LLM裁决，需人工判断"
                f"\n[OPTIMAL_DECISION]"
                f"\nside=hold verdict=HOLD lots=0 alloc_cny=0"
                f"\nreason=floating_loss_protected_optimizer_sell_blocked"
                f"\nblack_litterman_anchor_target={position_pct:.1f}%"
            )
            parsed["alloc_cny"] = 0
            parsed["target_position_pct"] = max(5.0, min(position_pct, 35.0))
        else:
            right_side_blocked = (
                position_pct <= 0
                and opt.verdict in {"BUY", "ACCUMULATE"}
                and not right_side_gate.allow
            )
            if right_side_blocked:
                cio_memo += (
                    f"\n[RIGHT_SIDE_GATE_BLOCK] {opt.verdict} -> HOLD "
                    f"reason={right_side_gate.reason}"
                )
                parsed["verdict"] = "HOLD"
                parsed["confidence"] = min(opt.confidence, 0.55)
                parsed["alloc_cny"] = 0
            else:
                if (
                    parsed.get("verdict") != opt.verdict
                    or int(parsed.get("alloc_cny", 0) or 0) != opt.alloc_cny
                ):
                    cio_memo += (
                        f"\n[OPTIMIZER_OVERRIDE] LLM={parsed.get('verdict')} "
                        f"alloc={parsed.get('alloc_cny', 0)} -> {opt.verdict} alloc={opt.alloc_cny}"
                    )
                parsed["verdict"] = opt.verdict
                parsed["confidence"] = opt.confidence
                parsed["alloc_cny"] = opt.alloc_cny
            cio_memo += opt_audit_text

        from core.buy_signal_miner import mine_historical_buy_signals
        buy_signal_backtest = mine_historical_buy_signals(symbol, df_2y)

        from core.decision_synthesis import synthesize_decision
        decision_synthesis = synthesize_decision(
            symbol=symbol,
            name=name,
            optimizer=opt,
            parsed=parsed,
            entry_exit_points=entry_exit_plan.as_dict(),
            right_side_gate=right_side_gate.as_dict(),
            position_exit_policy=position_exit_policy.as_dict(),
            optimizer_review=optimizer_review,
            current_price=current_price or 0.0,
            is_holding=(position_pct > 0),
        )
        cio_memo += decision_synthesis.audit_text()

        elapsed = (datetime.now() - t0).total_seconds()
        log.info(f"本地委员会完成: verdict={parsed['verdict']} confidence={parsed['confidence']:.2f} elapsed={elapsed:.1f}s")

        # 缓存到本地
        _save_committee_cache(
            symbol=symbol,
            name=name or symbol,
            current_price=current_price or 0,
            verdict=parsed.get("verdict", "UNCLEAR"),
            confidence=parsed.get("confidence", 0),
            suggested_alloc=parsed.get("alloc_cny", 0),
            quant_signal=report.quant_view[:200] if report.quant_view else "",
            regime=regime_brief[:500],
            fundamental_model=fundamental_assessment.model_key,
            fundamental_score=fundamental_assessment.score,
            entry_exit_points=entry_exit_plan.as_dict(),
            position_exit_policy=position_exit_policy.as_dict(),
            right_side_trend_gate=right_side_gate.as_dict(),
            optimizer_review=optimizer_review[:500],
            cio_note=cio_memo[:500],
        )

        formatted_text = build_mobile_recommendation_text(
            symbol=symbol,
            name=name,
            market=market,
            current_price=current_price or 0.0,
            change_pct=change_pct,
            verdict=parsed.get("verdict", "UNCLEAR"),
            confidence=parsed.get("confidence", 0.0),
            suggested_alloc_cny=float(parsed.get("alloc_cny", 0)),
            is_holding=(position_pct > 0),
            entry_exit_points=entry_exit_plan.as_dict(),
            position_exit_policy=position_exit_policy.as_dict(),
            right_side_trend_gate=right_side_gate.as_dict(),
            fundamental_score=float(fundamental_assessment.score),
            regime_brief=regime_brief,
            cio_memo=cio_memo,
            is_from_cache=False,
            decision_synthesis=decision_synthesis.as_dict(),
            buy_signal_backtest=buy_signal_backtest.as_dict(),
            behavioral_factor=behavioral_assessment.as_dict() if behavioral_assessment is not None else {},
        )

        response = {
            "success": True,
            "symbol": symbol,
            "name": name,
            "market": market,
            "verdict": parsed.get("verdict", "UNCLEAR"),
            "confidence": parsed.get("confidence", 0.0),
            "dominant_view": parsed.get("dominant_view", "tie"),
            "suggested_alloc_cny": float(parsed.get("alloc_cny", 0)),
            "cio_memo": cio_memo,
            "formatted_recommendation": formatted_text,
            "macro_view": report.macro_view or "",
            "quant_view": report.quant_view or "",
            "risk_view": report.risk_view or "",
            "quant_adjusted": report.quant_adjusted or "",
            "risk_adjusted": report.risk_adjusted or "",
            "market_data": market_data[:2000],
            "regime": regime_brief[:500],
            "fundamental_model": fundamental_assessment.model_key,
            "fundamental_score": float(fundamental_assessment.score),
            "fundamental_coverage": float(fundamental_assessment.coverage),
            "fundamental_anchor_multiplier": float(fundamental_assessment.anchor_multiplier),
            "entry_exit_points": entry_exit_plan.as_dict(),
            "position_exit_policy": position_exit_policy.as_dict(),
            "right_side_trend_gate": right_side_gate.as_dict(),
            "optimizer_review": optimizer_review,
            "decision_synthesis": decision_synthesis.as_dict(),
            "buy_signal_backtest": buy_signal_backtest.as_dict(),
            "behavioral_factor": behavioral_assessment.as_dict() if behavioral_assessment is not None else {},
            "elapsed_sec": round(elapsed, 1)
        }
        return json.dumps(response, ensure_ascii=False)

    except Exception as e:
        elapsed = (datetime.now() - t0).total_seconds()
        log.exception(f"本地手机端委员会分析失败: {e}")
        return json.dumps({
            "success": False,
            "symbol": symbol,
            "name": name,
            "market": market,
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "elapsed_sec": round(elapsed, 1),
        }, ensure_ascii=False)


def evaluate_alerts_local(
    results_json: str,
    prices_json: str,
    holding_symbols_json: str,
    stocks_json: str
) -> str:
    """
    Evaluate entry/exit and stop loss triggers using the exact python algorithms.
    Returns a JSON string of confirmed alerts and watch rows.
    """
    t0 = datetime.now()
    try:
        results = json.loads(results_json)
        prices = json.loads(prices_json)
        holding_symbols = set(json.loads(holding_symbols_json))
        stocks = json.loads(stocks_json)

        import tempfile
        state_path = Path(tempfile.gettempdir()) / "entry_exit_alert_state.json"

        from jobs.market_monitor_entry_exit import update_entry_exit_alert_state
        confirmed_alerts, watch_rows = update_entry_exit_alert_state(
            results=results,
            prices=prices,
            holding_symbols=holding_symbols,
            stocks=stocks,
            state_path=state_path
        )

        return json.dumps({
            "success": True,
            "confirmed_alerts": confirmed_alerts,
            "watch_rows": watch_rows,
            "elapsed_sec": (datetime.now() - t0).total_seconds()
        }, ensure_ascii=False)
    except Exception as e:
        log.exception(f"evaluate_alerts_local failed: {e}")
        return json.dumps({
            "success": False,
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "elapsed_sec": (datetime.now() - t0).total_seconds()
        }, ensure_ascii=False)
