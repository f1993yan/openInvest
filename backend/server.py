"""openInvest 后端服务 — FastAPI + akshare 国内数据源

启动: uv run uvicorn backend.server:app --host 0.0.0.0 --port 8766

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
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保项目根在 sys.path（uvicorn 启动时 workspaces 不同）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI
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


class CommitteeResponse(BaseModel):
    """委员会分析响应"""
    success: bool
    symbol: str
    name: str
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
    error: str = ""
    elapsed_sec: float = 0.0


# ==========================================
# 缓存
# ==========================================

import pickle as _pickle

CACHE_DIR = _PROJECT_ROOT / "data" / "committee_cache"


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
    )
    return await run_committee_api(req)


@app.post("/api/committee", response_model=CommitteeResponse)
async def run_committee_api(req: CommitteeRequest):
    """跑投资委员会分析"""
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
        if warnings:
            fundamental_brief += f"\nDATA_WARNINGS: {', '.join(warnings[:5])}"
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
        portfolio_summary += f"\n\n### 基本面数学模型锚点\n{fundamental_brief}"

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
            cio_memo += opt.audit_text()

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
            regime=regime_brief[:200],
            fundamental_model=fundamental_assessment.model_key,
            fundamental_score=fundamental_assessment.score,
            cio_note=cio_memo[:500],
        )

        return CommitteeResponse(
            success=True,
            symbol=req.symbol,
            name=req.name,
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
            elapsed_sec=round(elapsed, 1),
        )

    except Exception as e:
        elapsed = (datetime.now() - t0).total_seconds()
        log.exception(f"委员会分析失败: {e}")
        return CommitteeResponse(
            success=False,
            symbol=req.symbol,
            name=req.name,
            error=f"{type(e).__name__}: {str(e)[:300]}",
            elapsed_sec=round(elapsed, 1),
        )


# ==========================================
# 启动入口
# ==========================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8766, log_level="info")
