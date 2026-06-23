"""openInvest 后端服务 — FastAPI + akshare 国内数据源

启动: uv run uvicorn backend.server:app --host 0.0.0.0 --port <port>

端点:
  POST /api/committee   — 跑 4 角色委员会，返回完整投资备忘录
  GET  /api/health       — 健康检查
"""
from __future__ import annotations

# --- Windows GBK 编码修复（from __future__ 之后、其他 import 之前）---
import os as _os
import sys as _sys

if _sys.platform == "win32":
    # 强制 UTF-8 模式：影响 filesystem encoding、stdio encoding、subprocess 默认编码
    _os.environ.setdefault("PYTHONUTF8", "1")
    _os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    # 重新打开 stdio 为 UTF-8（对已启动进程有效）
    import io as _io
    try:
        if hasattr(_sys.stdout, "buffer"):
            _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")
        if hasattr(_sys.stderr, "buffer"):
            _sys.stderr = _io.TextIOWrapper(_sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

import logging
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

# 确保项目根在 sys.path（uvicorn 启动时 workspaces 不同）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 加载 .env（直接调用 run_committee_direct 时需要）
try:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("openinvest.backend")

app = FastAPI(
    title="openInvest Backend",
    description="投资委员会后端 — akshare 国内数据源",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================
# 请求/响应模型
# ==========================================

class Holding(BaseModel):
    """单个持仓"""
    symbol: str = Field(..., description="股票代码")
    name: str = Field("", description="股票名称")
    weight_pct: float = Field(..., description="仓位百分比")
    cost: float = Field(..., description="成本价")
    current_price: Optional[float] = Field(None, description="现价（不传则akshare查）")


class CommitteeRequest(BaseModel):
    """委员会分析请求"""
    symbol: str = Field(..., description="要分析的股票代码，A股6位/港股5位")
    name: str = Field("", description="股票名称")
    market: str = Field("a", description="市场: a=沪深, hk=港股")
    sector: str = Field("", description="板块/行业大类，用于匹配基本面模型")
    industry: str = Field("", description="细分行业，用于匹配基本面模型")
    position_pct: float = Field(0.0, description="该股仓位百分比")
    target_position_pct: Optional[float] = Field(None, description="Black-Litterman/战略目标仓位百分比；不传则锚定当前仓位")
    cost: float = Field(0.0, description="成本均价")
    current_price: Optional[float] = Field(None, description="现价(不传则akshare查询)")
    total_assets: float = Field(100000.0, description="总资产约(CNY)")
    cash: float = Field(0.0, description="可用现金(CNY)")
    holdings: List[Holding] = Field(default_factory=list, description="其他持仓")
    risk_preference: str = Field("moderate", description="风险偏好: conservative/moderate/aggressive")
    max_debate_rounds: int = Field(4, description="辩论轮数上限(1-4)")
    # --- 交易约束 ---
    min_lot_size: int = Field(100, description="最低交易单位(股). A股=100, 港股=100/200等. 0=不限制")
    t_plus_1: bool = Field(True, description="T+1交易规则. A股True, 港股False(T+0)")
    available_cash: float = Field(0.0, description="可用资金(CNY). 约束建议建仓金额上限")
    t2_pending_cash: float = Field(0.0, description="港股通T+2待交收资金(CNY). 卖出港股后T+2才到账，当日不可用")
    news_brief: str = Field("", description="当前新闻摘要，注入到宏观/CIO决策中")
    fundamentals: Dict[str, Any] = Field(default_factory=dict, description="基本面指标字典，如 roe/roic/revenue_growth/pe_ttm/pb 等")
    optimizer_review_enabled: bool = Field(True, description="是否让 LLM 对确定性优化器输出做审计评估")


class CommitteeResponse(BaseModel):
    """委员会分析响应"""
    success: bool
    symbol: str
    name: str
    market: str = "a"  # 回传请求的 market，供下游（如涨停拦截）区分 A股/港股/美股
    verdict: str = ""
    confidence: float = 0.0
    dominant_view: str = ""
    suggested_alloc_cny: float = 0.0
    cio_memo: str = ""
    macro_view: str = ""
    quant_view: str = ""
    risk_view: str = ""
    quant_adjusted: str = ""
    risk_adjusted: str = ""
    market_data: str = ""
    regime: str = ""
    fundamental_model: str = ""
    fundamental_score: float = 50.0
    fundamental_coverage: float = 0.0
    fundamental_anchor_multiplier: float = 1.0
    entry_exit_points: Dict[str, Any] = Field(default_factory=dict)
    position_exit_policy: Dict[str, Any] = Field(default_factory=dict)
    right_side_trend_gate: Dict[str, Any] = Field(default_factory=dict)
    optimizer_review: str = ""
    error: str = ""
    elapsed_sec: float = 0.0


class AccountTradeRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    direction: Literal["BUY", "SELL"]
    units: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    trade_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    note: Optional[str] = Field(None, max_length=512)


class AccountSnapshotRequest(BaseModel):
    prices: Dict[str, float] = Field(default_factory=dict)
    trade_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")


# ==========================================
# 缓存
# ==========================================

import pickle as _pickle

CACHE_DIR = _PROJECT_ROOT / "data" / "committee_cache"
MONITOR_CONFIG_PATH = _PROJECT_ROOT / "jobs" / "market_monitor_config.json"


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


# ==========================================
# 内部：数据准备
# ==========================================

def _build_portfolio_summary(req: CommitteeRequest) -> str:
    """从请求构建 portfolio_summary 文本（Risk Officer 输入）"""
    lines = [
        f"## 投资组合概览",
        f"- 总资产约: ¥{req.total_assets:,.0f}",
        f"- 可用现金: ¥{req.cash:,.0f} ({req.cash/req.total_assets*100:.1f}%)",
        f"- 持仓数: {len(req.holdings) + (1 if req.position_pct > 0 else 0)} 只",
        f"",
        f"### 当前持仓明细",
    ]

    # 目标股票
    lines.append(
        f"- **{req.name or req.symbol}** ({req.symbol}): "
        f"仓位 {req.position_pct:.1f}%, "
        f"成本 ¥{req.cost:.2f}, "
        f"现价 ¥{req.current_price or 'N/A'}, "
        f"集中度 {req.position_pct:.1f}%"
    )

    # 其他持仓
    for h in req.holdings:
        lines.append(
            f"- {h.name} ({h.symbol}): "
            f"仓位 {h.weight_pct:.1f}%, "
            f"成本 ¥{h.cost:.2f}, "
            f"集中度 {h.weight_pct:.1f}%"
        )

    # 新闻摘要（注入到宏观 + CIO 决策中）
    if req.news_brief:
        lines.append("")
        lines.append("### 📰 当前新闻摘要（影响宏观判断和CIO决策）")
        lines.append(req.news_brief)

    # 交易约束（注入到 Risk Officer + CIO prompt 中）
    if req.min_lot_size > 0 or req.t_plus_1 or req.available_cash > 0:
        # 计算每手价格（整手约束的核心）
        price = req.current_price or 0
        if price > 0 and req.min_lot_size > 0:
            per_hand = price * req.min_lot_size
            max_hands = int(req.available_cash / per_hand) if req.available_cash > 0 and per_hand > 0 else 0
        else:
            per_hand = 0
            max_hands = 0

        lines.append("")
        lines.append("### ⚠️ 交易约束（CIO必须严格遵守，违反则建议无效）")
        if per_hand > 0:
            lines.append(f"- 🔴 **1手 = {req.min_lot_size}股 ≈ ¥{per_hand:,.0f}**（= 当前价 ¥{price:.2f} × {req.min_lot_size}）")
            lines.append(f"- 🔴 **可用资金最多买 {max_hands} 手**（¥{req.available_cash:,.0f} ÷ ¥{per_hand:,.0f}）")
            lines.append(f"- 🔴 **suggested_alloc_cny 必须 = ¥{per_hand:,.0f} × N手**（N为整数，且N ≤ {max_hands}）")
            lines.append(f"- 🔴 **禁止出现金额不够1手的建议**（< ¥{per_hand:,.0f} 的建议直接无效）")
        if req.t_plus_1:
            lines.append("- 🔴 **T+1 交易规则**: A股当日买入次日方可卖出")
        if req.market == "hk":
            lines.append("- 🔴 **港股通T+2交收**: 卖出港股后资金T+2才到账，当日不可用于买入A股")
            if req.t2_pending_cash > 0:
                lines.append(f"  ⚠️ 当前T+2待交收: ¥{req.t2_pending_cash:,.0f}（不可用）")
        if req.symbol.startswith("688") and req.position_pct == 0:
            lines.append("- 🔴 **科创板首次买入: 最低200股（2手）**，之后可100股递增")

    return "\n".join(lines)


def _resolve_market_data(symbol: str, market: str) -> str:
    """从 akshare 拉行情 + 计算指标 + regime，返回格式化的市场数据字符串"""
    from utils.akshare_data import get_history_data, analyze_multi_timeframe, get_macro_data
    from utils.market_metrics import compute_metrics
    from core.regime import format_regime_brief

    df_2y = get_history_data(symbol, "2y")
    if df_2y.empty:
        return "市场数据暂缺（akshare 查询失败）"

    # 多时间框架报告
    tech_report = analyze_multi_timeframe(df_2y, symbol)

    # Regime 判定
    metrics = compute_metrics(df_2y)
    regime_brief = format_regime_brief(metrics, symbol=symbol)

    # 宏观
    macro_data = get_macro_data()

    return f"{macro_data}\n\n--- 技术分析 ---\n{tech_report}\n\n--- REGIME ---\n{regime_brief}"


# ==========================================
# API 端点
# ==========================================

@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


_account_ledger = None


def _get_account_ledger():
    global _account_ledger
    if _account_ledger is None:
        from db.account_ledger import AccountLedger
        _account_ledger = AccountLedger()
        try:
            if MONITOR_CONFIG_PATH.exists():
                _account_ledger.ensure_initialized(json.loads(_read_text_fallback(MONITOR_CONFIG_PATH)))
        except Exception as e:  # noqa: BLE001
            log.warning(f"双账户账本初始化失败: {e}")
    return _account_ledger


def _read_text_fallback(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "cp936"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


@app.get("/api/accounts")
async def list_dual_accounts() -> Dict[str, Any]:
    """Return real account and committee shadow-account state."""
    db = _get_account_ledger()
    return {
        "accounts": {
            "real": {
                "summary": db.account_summary("real"),
                "holdings": db.list_holdings("real"),
            },
            "committee": {
                "summary": db.account_summary("committee"),
                "holdings": db.list_holdings("committee"),
            },
        }
    }


@app.get("/api/accounts/trades")
async def list_account_trades(
    account: Optional[Literal["real", "committee"]] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    rows = _get_account_ledger().list_trades(account=account, limit=limit)
    return {"count": len(rows), "rows": rows}


@app.get("/api/accounts/pnl")
async def list_account_pnl(
    account: Optional[Literal["real", "committee"]] = Query(None),
    limit: int = Query(60, ge=1, le=500),
) -> Dict[str, Any]:
    rows = _get_account_ledger().list_daily_pnl(account=account, limit=limit)
    return {"count": len(rows), "rows": rows}


@app.post("/api/accounts/snapshot")
async def snapshot_accounts(body: AccountSnapshotRequest = Body(default=AccountSnapshotRequest())) -> Dict[str, Any]:
    rows = _get_account_ledger().snapshot_daily_pnl(
        prices=body.prices,
        trade_date=body.trade_date,
    )
    return {"ok": True, "rows": rows}


@app.post("/api/accounts/real/trades")
async def record_real_account_trade(body: AccountTradeRequest = Body(...)) -> Dict[str, Any]:
    try:
        trade = _get_account_ledger().apply_user_trade(
            symbol=body.symbol,
            direction=body.direction,
            units=body.units,
            price=body.price,
            trade_date=body.trade_date,
            note=body.note or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "trade": trade.__dict__}


@app.get("/api/committee")
async def run_committee_get(
    symbol: str,
    name: str = "",
    market: str = "a",
    sector: str = "",
    industry: str = "",
    position_pct: float = 0.0,
    target_position_pct: Optional[float] = None,
    cost: float = 0.0,
    current_price: Optional[float] = None,
    total_assets: float = 100000.0,
    cash: float = 0.0,
    max_debate_rounds: int = 4,
    min_lot_size: int = 100,
    t_plus_1: bool = True,
    available_cash: float = 0.0,
    t2_pending_cash: float = 0.0,
    optimizer_review_enabled: bool = True,
):
    """GET 版本的委员会分析（方便 web_fetch 调用）"""
    req = CommitteeRequest(
        symbol=symbol,
        name=name,
        market=market,
        sector=sector,
        industry=industry,
        position_pct=position_pct,
        target_position_pct=target_position_pct,
        cost=cost,
        current_price=current_price,
        total_assets=total_assets,
        cash=cash,
        max_debate_rounds=max_debate_rounds,
        min_lot_size=min_lot_size,
        t_plus_1=t_plus_1,
        available_cash=available_cash,
        t2_pending_cash=t2_pending_cash,
        optimizer_review_enabled=optimizer_review_enabled,
    )
    return await run_committee_api(req)


def run_committee_direct(req: CommitteeRequest) -> CommitteeResponse:
    """无 HTTP 调用的委员会分析入口——供 market_monitor / weekend_news 直接 Python 调用。

    逻辑与 POST /api/committee 完全相同（同一份代码），
    只是不经过 FastAPI / uvicorn，不监听端口。
    """
    t0 = datetime.now()

    try:
        # 0. 科创板自动检测：688xxx 首次买入最低200股
        if req.symbol.startswith("688") and req.position_pct == 0:
            req.min_lot_size = max(req.min_lot_size, 200)

        # 1. 查现价
        current_price = req.current_price
        if current_price is None:
            try:
                from utils.akshare_data import get_history_data
                df = get_history_data(req.symbol, "5d")
                if not df.empty:
                    current_price = float(df["Close"].iloc[-1])
            except Exception:
                current_price = 0.0

        # 2. 拉市场数据
        log.info(f"拉取 {req.symbol} 市场数据...")
        market_data = _resolve_market_data(req.symbol, req.market)

        # 3. 拉 regime brief（从 market_data 提取或重算）
        from utils.akshare_data import get_history_data
        from utils.market_metrics import compute_metrics
        from core.regime import format_regime_brief
        df_2y = get_history_data(req.symbol, "2y")
        metrics = compute_metrics(df_2y) if not df_2y.empty else {}
        regime_brief = format_regime_brief(metrics, symbol=req.symbol)

        from core.fundamental_model import assess_fundamentals
        from utils.fundamental_data import get_fundamental_snapshot

        fundamental_snapshot = get_fundamental_snapshot(req.symbol, req.market)
        auto_fundamentals = fundamental_snapshot.get("metrics", {})
        merged_fundamentals = {**auto_fundamentals, **(req.fundamentals or {})}
        fundamental_assessment = assess_fundamentals(
            symbol=req.symbol,
            name=req.name,
            sector=req.sector,
            industry=req.industry,
            market=req.market,
            metrics=merged_fundamentals,
        )
        fundamental_brief = fundamental_assessment.summary_text()
        if fundamental_snapshot.get("cache_hit"):
            cache_state = "stale_cache" if fundamental_snapshot.get("cache_stale") else "cache_hit"
        else:
            cache_state = "live_fetch"
        fundamental_brief += (
            f"\nDATA_SOURCE: {fundamental_snapshot.get('source', 'unknown')} "
            f"{cache_state} metrics={len(auto_fundamentals)} "
            f"manual_overrides={len(req.fundamentals or {})}"
        )
        warnings = fundamental_snapshot.get("warnings") or []
        critical_warnings = [
            w for w in warnings
            if "ConnectionError" not in w and "failed" not in w
        ]
        if critical_warnings:
            fundamental_brief += f"\nDATA_WARNINGS: {', '.join(critical_warnings[:5])}"
        market_data += f"\n\n--- FUNDAMENTAL MODEL ---\n{fundamental_brief}"

        # 4. 获取宏观视图（含新闻）
        from utils.akshare_data import get_macro_data
        macro_data = get_macro_data()
        if req.news_brief:
            macro_data += f"\n\n## 当前新闻\n{req.news_brief}"
        from core.committee import run_macro_view
        macro_view = run_macro_view(macro_data)
        log.info(f"宏观: {macro_view[:120]}...")

        # 5. 构建输入
        asset = {
            "symbol": req.symbol,
            "display_name": req.name or req.symbol,
        }
        portfolio_summary = _build_portfolio_summary(req)
        from core.position_exit_policy import load_position_exit_policy
        position_exit_policy = load_position_exit_policy(sector=req.sector)
        position_exit_policy_text = position_exit_policy.audit_text(
            symbol=req.symbol,
            market=req.market,
            is_holding=req.position_pct > 0,
            cost=req.cost,
            current_price=current_price or 0.0,
        )
        portfolio_summary += f"\n\n### 基本面数学模型锚点\n{fundamental_brief}"
        portfolio_summary += f"\n\n### 已持仓A股止盈止损纪律（不要当作入场点）\n{position_exit_policy_text}"

        # 构建 wealth_context_view stub（无 user.md 时用）
        from core.committee import run_wealth_context_view
        wealth_context = run_wealth_context_view(None, req.cash)

        # 6. 跑委员会
        log.info(f"启动委员会辩论 (max rounds={req.max_debate_rounds})...")
        from core.committee import run_committee, parse_cio_memo

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
            max_debate_rounds=min(req.max_debate_rounds, 4),
        )

        # 7. 解析 verdict
        report = result.get("report")
        if report is None:
            return CommitteeResponse(
                success=False,
                symbol=req.symbol,
                name=req.name,
                error=f"委员会返回空 report: {result.get('error', 'unknown')}",
                elapsed_sec=(datetime.now() - t0).total_seconds(),
            )

        cio_memo = report.cio_memo or ""
        parsed = parse_cio_memo(cio_memo, current_price=current_price)

        # 7.5 后端确定性执行优化：LLM 只给弱先验，最终动作/手数由期望效用决定
        atr_pct = metrics.get("atr_pct") if metrics else None
        if atr_pct and current_price and current_price > 0:
            atr_amount = current_price * atr_pct / 100
            # 2N止损 + 3N止盈（1.5:1盈亏比）
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
                prob_table = build_probability_table_from_ohlc([req.symbol])
                regime_probability = get_regime_probability(
                    req.symbol.upper(), regime_label, table=prob_table,
                )
                conditional_return_stats = get_conditional_return_stats(
                    req.symbol.upper(), regime_label,
                )
        except Exception as e:  # noqa: BLE001
            log.warning(f"regime 概率表不可用，执行优化退化为指标模型: {type(e).__name__}: {e}")

        from core.decision_optimizer import optimize_committee_decision
        from core.entry_exit_points import compute_entry_exit_points

        opt = optimize_committee_decision(
            parsed=parsed,
            metrics=metrics,
            symbol=req.symbol,
            regime_brief=regime_brief,
            current_price=current_price,
            total_assets=req.total_assets,
            available_cash=req.available_cash if req.available_cash > 0 else req.cash,
            position_pct=req.position_pct,
            min_lot_size=req.min_lot_size,
            bl_anchor_target_pct=req.target_position_pct if hasattr(req, 'target_position_pct') else None,
            market=req.market,
            risk_preference=req.risk_preference,
            regime_probability=regime_probability,
            conditional_return_stats=conditional_return_stats,
            fundamental_assessment=fundamental_assessment,
        )
        entry_exit_plan = compute_entry_exit_points(
            symbol=req.symbol,
            current_price=current_price,
            metrics=metrics,
            regime_brief=regime_brief,
            market=req.market,
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
            market=req.market,
            is_holding=req.position_pct > 0,
        )
        opt_audit_text = opt.audit_text()
        entry_exit_audit_text = entry_exit_plan.audit_text()
        right_side_gate_text = right_side_gate.audit_text()
        optimizer_review = ""
        if req.optimizer_review_enabled:
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
            except Exception as e:  # noqa: BLE001
                optimizer_review = (
                    "[WORKER_UNAVAILABLE] "
                    f"reason=optimizer_review_failed exc_type={type(e).__name__}"
                )
        # 浮亏保护：持仓亏损时，优化器SELL/TRIM不自动覆盖LLM
        has_loss = (
            req.position_pct > 0
            and req.cost > 0
            and current_price
            and current_price < req.cost
        )
        if has_loss and opt.verdict in ("SELL", "TRIM"):
            cio_memo += (
                f"\n[FLOATING_LOSS_PROTECT] 持仓浮亏({(current_price/req.cost-1)*100:.1f}%)，"
                f"优化器{opt.verdict}→保留LLM裁决，需人工判断"
                f"\n[OPTIMAL_DECISION]"
                f"\nside=hold verdict=HOLD lots=0 alloc_cny=0"
                f"\nreason=floating_loss_protected_optimizer_sell_blocked"
                f"\nblack_litterman_anchor_target={req.position_pct:.1f}%"
            )
            parsed["alloc_cny"] = 0
            # 让LLM分析技术面给出target_position_pct
            parsed["target_position_pct"] = max(5.0, min(req.position_pct, 35.0))
        else:
            right_side_blocked = (
                req.position_pct <= 0
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
        cio_memo += entry_exit_audit_text
        cio_memo += right_side_gate_text
        cio_memo += position_exit_policy_text
        if optimizer_review:
            cio_memo += f"\n\n[OPTIMIZER_LLM_REVIEW]\n{optimizer_review}"

        elapsed = (datetime.now() - t0).total_seconds()
        log.info(f"委员会完成: verdict={parsed['verdict']} confidence={parsed['confidence']:.2f} elapsed={elapsed:.1f}s")

        # 8. 缓存到本地（二进制 pickle）
        _save_committee_cache(
            symbol=req.symbol,
            name=req.name or req.symbol,
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

        return CommitteeResponse(
            success=True,
            symbol=req.symbol,
            name=req.name,
            market=req.market,
            verdict=parsed.get("verdict", "UNCLEAR"),
            confidence=parsed.get("confidence", 0.0),
            dominant_view=parsed.get("dominant_view", "tie"),
            suggested_alloc_cny=parsed.get("alloc_cny", 0),
            cio_memo=cio_memo,
            macro_view=report.macro_view or "",
            quant_view=report.quant_view or "",
            risk_view=report.risk_view or "",
            quant_adjusted=report.quant_adjusted or "",
            risk_adjusted=report.risk_adjusted or "",
            market_data=market_data[:2000],
            regime=regime_brief[:500],
            fundamental_model=fundamental_assessment.model_key,
            fundamental_score=fundamental_assessment.score,
            fundamental_coverage=fundamental_assessment.coverage,
            fundamental_anchor_multiplier=fundamental_assessment.anchor_multiplier,
            entry_exit_points=entry_exit_plan.as_dict(),
            position_exit_policy=position_exit_policy.as_dict(),
            right_side_trend_gate=right_side_gate.as_dict(),
            optimizer_review=optimizer_review,
            elapsed_sec=round(elapsed, 1),
        )

    except Exception as e:
        elapsed = (datetime.now() - t0).total_seconds()
        log.exception(f"委员会分析失败: {e}")
        return CommitteeResponse(
            success=False,
            symbol=req.symbol,
            name=req.name,
            market=req.market,
            error=f"{type(e).__name__}: {str(e)[:300]}",
            elapsed_sec=round(elapsed, 1),
        )


class CrawlerSettings(BaseModel):
    frequency_minutes: int = Field(60, ge=1, description="爬网频率（分钟）")
    target_refresh_enabled: bool = Field(True, description="开启标的刷新")
    news_refresh_enabled: bool = Field(True, description="开启周末新闻")

@app.post("/api/committee", response_model=CommitteeResponse)
async def run_committee_api(req: CommitteeRequest):
    """跑投资委员会分析（HTTP 端点，委托给 run_committee_direct）"""
    return run_committee_direct(req)


@app.get("/api/monitor/snapshot")
async def get_monitor_snapshot():
    """获取标的监控快照数据"""
    try:
        from scripts.monitor_window_services import _load_snapshot
        from scripts.monitor_window_constants import DEFAULT_SNAPSHOT
        return _load_snapshot(DEFAULT_SNAPSHOT)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load snapshot: {str(e)}")


@app.get("/api/monitor/selection")
async def get_monitor_selection():
    """获取日度选股快照数据"""
    try:
        from scripts.monitor_window_services import _load_daily_selection
        from scripts.monitor_window_constants import DAILY_SELECTION_LATEST
        return _load_daily_selection(DAILY_SELECTION_LATEST)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load daily selection: {str(e)}")


@app.get("/api/monitor/news")
async def get_monitor_news():
    """获取周末新闻卡片数据"""
    try:
        from scripts.monitor_window_services import _load_weekend_news_cards
        cards, filename = _load_weekend_news_cards()
        return {"cards": cards, "filename": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load news: {str(e)}")

@app.post("/api/config/crawler")
async def update_crawler_config(settings: CrawlerSettings):
    """更新远程数据获取配置"""
    try:
        settings_path = _PROJECT_ROOT / "jobs" / "crawler_settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "settings": settings.model_dump()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save settings: {str(e)}")


@app.get("/api/config/crawler")
async def get_crawler_config():
    """获取远程数据获取配置"""
    try:
        settings_path = _PROJECT_ROOT / "jobs" / "crawler_settings.json"
        if settings_path.exists():
            return json.loads(settings_path.read_text(encoding="utf-8"))
        return {
            "frequency_minutes": 60,
            "target_refresh_enabled": True,
            "news_refresh_enabled": True
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load settings: {str(e)}")


@app.post("/api/config/monitor_config")
async def import_monitor_config(config: Dict[str, Any] = Body(...)):
    """导入/覆盖仓位及自选标的配置文件 (market_monitor_config.json)"""
    try:
        MONITOR_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        MONITOR_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            # Re-initialize account ledger dynamically
            from db.account_ledger import get_account_ledger
            _ledger = get_account_ledger()
            _ledger.initialize_from_monitor_config(config, reset=True)
            log.info("Ledger re-initialized with new imported monitor config (reset=True)")
        except Exception as le:
            log.warning(f"Failed to re-initialize ledger after import: {le}")
            
        try:
            # Delete stale snapshot so that it is rebuilt from the new config
            from scripts.monitor_window_constants import DEFAULT_SNAPSHOT
            if DEFAULT_SNAPSHOT.exists():
                DEFAULT_SNAPSHOT.unlink()
                log.info("Stale snapshot file deleted after config import")
        except Exception as se:
            log.warning(f"Failed to delete stale snapshot file: {se}")
            
        return {"ok": True, "message": "Monitor configuration imported successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save monitor config: {str(e)}")



@app.get("/api/config/monitor_config")
async def get_monitor_config():
    """获取当前的仓位配置文件内容"""
    try:
        if MONITOR_CONFIG_PATH.exists():
            return json.loads(MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="Monitor config file not found")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to read monitor config: {str(e)}")


# ============ Holdings / Watchlist CRUD ============

@app.post("/api/holdings")
async def add_holding_api(body: Dict[str, Any] = Body(...)):
    """新增持仓或自选标的。如果 is_tracking_only=True 则放入 watchlist，否则放入 holdings。"""
    try:
        symbol = str(body.get("symbol") or "").strip().upper()
        if not symbol:
            raise HTTPException(status_code=400, detail="symbol 不能为空")
        
        name = str(body.get("display_name") or body.get("name") or symbol).strip()
        is_tracking_only = bool(body.get("is_tracking_only", True))
        sector = str(body.get("channel") or body.get("sector") or "").strip()
        market = str(body.get("market") or "a").strip().lower()
        units = float(body.get("units") or 0.0)
        cost = float(body.get("avg_cost") or body.get("cost") or 0.0)
        
        if not MONITOR_CONFIG_PATH.exists():
            raise HTTPException(status_code=500, detail="配置文件不存在")
        
        config = json.loads(MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
        holdings = config.get("holdings", [])
        watchlist = config.get("watchlist", [])
        
        # Check if already exists (case-insensitive)
        in_holdings = any(str(h.get("symbol")).strip().upper() == symbol for h in holdings)
        in_watchlist = any(str(w.get("symbol")).strip().upper() == symbol for w in watchlist)
        
        if in_holdings or in_watchlist:
            return {"ok": True, "message": "已在持仓或自选列表中", "duplicate": True}
        
        if is_tracking_only:
            # Add to watchlist
            watchlist.append({
                "symbol": symbol,
                "name": name,
                "market": market,
                "sector": sector,
                "industry": sector,
                "position_pct": 0.0,
                "cost": 0.0
            })
        else:
            # Add to holdings
            holdings.append({
                "symbol": symbol,
                "name": name,
                "market": market,
                "sector": sector,
                "industry": sector,
                "position_pct": 0.0,
                "cost": cost,
                "units": units,
                "min_lot_size": 100
            })
            
        config["holdings"] = holdings
        config["watchlist"] = watchlist
        
        # Save config
        MONITOR_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        
        # Re-initialize ledger
        try:
            from db.account_ledger import get_account_ledger
            _ledger = get_account_ledger()
            _ledger.initialize_from_monitor_config(config, reset=True)
            log.info(f"Ledger re-initialized after adding symbol: {symbol}")
        except Exception as le:
            log.warning(f"Failed to re-initialize ledger: {le}")
            
        # Delete stale snapshot
        try:
            from scripts.monitor_window_constants import DEFAULT_SNAPSHOT
            if DEFAULT_SNAPSHOT.exists():
                DEFAULT_SNAPSHOT.unlink()
                log.info("Stale snapshot deleted after adding symbol")
        except Exception as se:
            log.warning(f"Failed to delete snapshot: {se}")
            
        return {"ok": True, "message": "已加入关注列表" if is_tracking_only else "已加入持仓列表"}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to add holding: {str(e)}")


@app.put("/api/holdings/{symbol}")
async def update_holding_api(symbol: str, body: Dict[str, Any] = Body(...)):
    """更新持仓或自选状态。如果 is_tracking_only=True 则将标的移入/保留在 watchlist，否则移入/保留在 holdings。"""
    try:
        symbol = symbol.strip().upper()
        units = float(body.get("units") or 0.0)
        cost = float(body.get("avg_cost") or body.get("cost") or 0.0)
        is_tracking_only = bool(body.get("is_tracking_only", True))
        
        if not MONITOR_CONFIG_PATH.exists():
            raise HTTPException(status_code=500, detail="配置文件不存在")
            
        config = json.loads(MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
        holdings = config.get("holdings", [])
        watchlist = config.get("watchlist", [])
        
        # Find item in either list
        target_item = None
        found_in_holdings = False
        
        for h in holdings:
            if str(h.get("symbol")).strip().upper() == symbol:
                target_item = h
                found_in_holdings = True
                break
        
        if not target_item:
            for w in watchlist:
                if str(w.get("symbol")).strip().upper() == symbol:
                    target_item = w
                    found_in_holdings = False
                    break
                    
        if not target_item:
            raise HTTPException(status_code=404, detail=f"未找到标的: {symbol}")
            
        # Remove target_item from its current list
        if found_in_holdings:
            holdings = [h for h in holdings if str(h.get("symbol")).strip().upper() != symbol]
        else:
            watchlist = [w for w in watchlist if str(w.get("symbol")).strip().upper() != symbol]
            
        # Update fields
        target_item["units"] = units
        target_item["cost"] = cost
        
        # Calculate position_pct if moving to holdings and we have total assets
        total_assets = config.get("total_assets", 0.0)
        if not is_tracking_only and total_assets > 0:
            target_item["position_pct"] = (units * cost) / total_assets * 100.0
        else:
            target_item["position_pct"] = 0.0
            
        if is_tracking_only:
            # Move to / keep in watchlist
            target_item.pop("units", None)
            target_item["cost"] = 0.0
            target_item["position_pct"] = 0.0
            watchlist.append(target_item)
        else:
            # Move to / keep in holdings
            if "min_lot_size" not in target_item:
                target_item["min_lot_size"] = 100
            holdings.append(target_item)
            
        config["holdings"] = holdings
        config["watchlist"] = watchlist
        
        # Save config
        MONITOR_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        
        # Re-initialize ledger
        try:
            from db.account_ledger import get_account_ledger
            _ledger = get_account_ledger()
            _ledger.initialize_from_monitor_config(config, reset=True)
            log.info(f"Ledger re-initialized after updating symbol: {symbol}")
        except Exception as le:
            log.warning(f"Failed to re-initialize ledger: {le}")
            
        # Delete stale snapshot
        try:
            from scripts.monitor_window_constants import DEFAULT_SNAPSHOT
            if DEFAULT_SNAPSHOT.exists():
                DEFAULT_SNAPSHOT.unlink()
                log.info("Stale snapshot deleted after updating symbol")
        except Exception as se:
            log.warning(f"Failed to delete snapshot: {se}")
            
        return {"ok": True, "message": "持仓数据更新成功"}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to update holding: {str(e)}")


@app.delete("/api/holdings/{symbol}")
async def delete_holding_api(symbol: str):
    """删除持仓或自选标的。"""
    try:
        symbol = symbol.strip().upper()
        if not MONITOR_CONFIG_PATH.exists():
            raise HTTPException(status_code=500, detail="配置文件不存在")
            
        config = json.loads(MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
        holdings = config.get("holdings", [])
        watchlist = config.get("watchlist", [])
        
        # Find item in either list
        in_holdings = any(str(h.get("symbol")).strip().upper() == symbol for h in holdings)
        in_watchlist = any(str(w.get("symbol")).strip().upper() == symbol for w in watchlist)
        
        if not in_holdings and not in_watchlist:
            raise HTTPException(status_code=404, detail=f"标的 {symbol} 不存在")
            
        if in_holdings:
            # Check if units > 0 (avoid deleting real assets with units)
            target = next(h for h in holdings if str(h.get("symbol")).strip().upper() == symbol)
            if float(target.get("units", 0) or 0) > 0:
                raise HTTPException(status_code=400, detail="持仓股数大于0，不能直接删除。请先平仓或设股数为0")
            holdings = [h for h in holdings if str(h.get("symbol")).strip().upper() != symbol]
        else:
            watchlist = [w for w in watchlist if str(w.get("symbol")).strip().upper() != symbol]
            
        config["holdings"] = holdings
        config["watchlist"] = watchlist
        
        # Save config
        MONITOR_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        
        # Re-initialize ledger
        try:
            from db.account_ledger import get_account_ledger
            _ledger = get_account_ledger()
            _ledger.initialize_from_monitor_config(config, reset=True)
            log.info(f"Ledger re-initialized after deleting symbol: {symbol}")
        except Exception as le:
            log.warning(f"Failed to re-initialize ledger: {le}")
            
        # Delete stale snapshot
        try:
            from scripts.monitor_window_constants import DEFAULT_SNAPSHOT
            if DEFAULT_SNAPSHOT.exists():
                DEFAULT_SNAPSHOT.unlink()
                log.info("Stale snapshot deleted after deleting symbol")
        except Exception as se:
            log.warning(f"Failed to delete snapshot: {se}")
            
        return {"ok": True, "message": "已删除关注"}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to delete holding: {str(e)}")


@app.post("/api/config/exit_params")
async def import_exit_params(params: Dict[str, Any] = Body(...)):
    """导入/覆盖每周止盈参数配置文件 (weekly_exit_param_optimization.json)"""
    try:
        exit_path = _PROJECT_ROOT / "reports" / "weekly_exit_param_optimization.json"
        exit_path.parent.mkdir(parents=True, exist_ok=True)
        exit_path.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "message": "Weekly exit parameter optimization file imported successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save exit parameters: {str(e)}")


@app.get("/api/config/exit_params")
async def get_exit_params():
    """获取每周止盈参数配置文件内容"""
    try:
        exit_path = _PROJECT_ROOT / "reports" / "weekly_exit_param_optimization.json"
        if exit_path.exists():
            return json.loads(exit_path.read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="Exit parameters file not found")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to read exit parameters: {str(e)}")


@app.get("/api/stock/history")
async def get_stock_history(symbol: str, period: str = "2y"):
    """获取股票的历史OHLC行情数据，供手机端本地分析"""
    try:
        from utils.akshare_data import get_history_data
        df = get_history_data(symbol, period)
        if df.empty:
            return {"success": False, "error": "No history data found"}
        df_reset = df.reset_index()
        if "Date" in df_reset.columns:
            df_reset["Date"] = df_reset["Date"].dt.strftime("%Y-%m-%d")
        data = df_reset.to_dict(orient="records")
        return {"success": True, "symbol": symbol, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/stock/fundamental")
async def get_stock_fundamental(symbol: str, market: str = "a"):
    """获取股票基本面数据，供手机端本地分析"""
    try:
        from utils.fundamental_data import get_fundamental_snapshot
        snapshot = get_fundamental_snapshot(symbol, market)
        return {"success": True, "symbol": symbol, "snapshot": snapshot}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/stock/macro")
async def get_macro_snapshot_api():
    """获取宏观行情数据快照，供手机端本地分析"""
    try:
        from utils.akshare_data import get_macro_snapshot
        snap = get_macro_snapshot()
        return {"success": True, "macro_data": snap}
    except Exception as e:
        return {"success": False, "error": str(e)}


def cleanup_old_data():
    """清理两个月（60天）以上的价格缓存和监控快照文件"""
    try:
        from datetime import datetime, timedelta
        cutoff_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        
        # 1. 清理 SQLite 数据价格缓存
        from db.market_store import MarketStore
        store = MarketStore()
        with store._lock:
            cursor = store.conn.cursor()
            cursor.execute("DELETE FROM daily_prices WHERE date < ?", (cutoff_date,))
            deleted_rows = cursor.rowcount
            store.conn.commit()
            log.info(f"数据库清理：已删除 {deleted_rows} 行 {cutoff_date} 之前的价格缓存。")
            
        # 2. 清理临时委员会缓存文件
        cache_dir = _PROJECT_ROOT / "data" / "committee_cache"
        if cache_dir.exists():
            for date_dir in cache_dir.iterdir():
                if date_dir.is_dir():
                    try:
                        dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
                        if datetime.now() - dir_date > timedelta(days=60):
                            import shutil
                            shutil.rmtree(date_dir)
                            log.info(f"文件清理：已删除超过2个月的委员会缓存目录 {date_dir.name}")
                    except ValueError:
                        pass
    except Exception as e:
        log.error(f"清理旧数据失败: {e}")


@app.on_event("startup")
async def startup_event():
    """服务启动时触发清理逻辑"""
    cleanup_old_data()


# ==========================================
# 启动入口
# ==========================================

if __name__ == "__main__":
    import uvicorn
    port = int(_os.getenv("INVEST_BACKEND_PORT", "0"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
