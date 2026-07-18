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
    # 原地调整编码，不替换/关闭宿主进程（pytest、IDE、桌面启动器）的捕获流。
    try:
        for _stream in (_sys.stdout, _sys.stderr):
            if hasattr(_stream, "reconfigure"):
                _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import logging
import hmac
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

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("openinvest.backend")

app = FastAPI(
    title="openInvest Backend",
    description="投资委员会后端 — akshare 国内数据源",
    version="0.1.0",
)

_cors_origins_raw = _os.getenv(
    "INVEST_CORS_ORIGINS",
    "http://127.0.0.1:8765,http://localhost:8765",
)
_cors_origins = [item.strip() for item in _cors_origins_raw.split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def require_optional_api_token(request: Request, call_next):
    """Protect every non-health route when INVEST_API_TOKEN is configured."""
    expected = str(_os.getenv("INVEST_API_TOKEN") or "").strip()
    if not expected or request.url.path == "/api/health" or request.method == "OPTIONS":
        return await call_next(request)
    auth = str(request.headers.get("Authorization") or "")
    scheme, _, supplied = auth.partition(" ")
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, expected):
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


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
    behavioral_factor: Dict[str, Any] = Field(
        default_factory=dict,
        description="可选的A股行为因子横截面评估；不传时服务端使用单标的低置信度降级评估",
    )
    # === 影子账户（Committee）字段 ===
    shadow_position_pct: float = Field(0.0, description="影子账户该股仓位百分比")
    shadow_cost: float = Field(0.0, description="影子账户成本均价")
    shadow_cash: float = Field(0.0, description="影子账户可用现金(CNY)")
    shadow_holdings: List[Holding] = Field(default_factory=list, description="影子账户其他持仓")
    shadow_available_cash: float = Field(0.0, description="影子账户可用资金(CNY)")
    shadow_t2_pending_cash: float = Field(0.0, description="影子账户港股通T+2待交收资金(CNY)")


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
    decision_synthesis: Dict[str, Any] = Field(default_factory=dict)
    buy_signal_backtest: Dict[str, Any] = Field(default_factory=dict)
    behavioral_factor: Dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    elapsed_sec: float = 0.0
    # === 影子账户结果 ===
    shadow_result: Optional[Dict[str, Any]] = Field(None, description="影子账户的独立评估结果（对用户隐藏）")



class AccountTradeRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    direction: Literal["BUY", "SELL"]
    units: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    trade_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    note: Optional[str] = Field(None, max_length=512)
    decision_id: Optional[str] = Field(None, min_length=8, max_length=128)
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=256)


class DecisionResponseRequest(BaseModel):
    accepted: bool
    reason: Optional[str] = Field(None, max_length=512)


class AccountSnapshotRequest(BaseModel):
    prices: Dict[str, float] = Field(default_factory=dict)
    trade_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class CashCorrectionRequest(BaseModel):
    cash: float = Field(..., ge=0, description="可用现金(CNY)")
    t2_pending_cash: Optional[float] = Field(None, ge=0, description="T+2 待交收现金(CNY)")


# ==========================================
# 缓存
# ==========================================

import pickle as _pickle

CACHE_DIR = _PROJECT_ROOT / "data" / "committee_cache"
MONITOR_CONFIG_PATH = _PROJECT_ROOT / "jobs" / "market_monitor_config.json"


def _behavioral_universe_rows(
    symbol: str,
    *,
    name: str = "",
    holdings: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    """Collect target, account holdings, and configured watchlist for factor repair."""
    rows: List[Dict[str, Any]] = [{"symbol": symbol, "name": name or symbol, "market": "a"}]

    def append_row(item: Any) -> None:
        if isinstance(item, dict):
            value = item
        else:
            value = {
                "symbol": getattr(item, "symbol", ""),
                "name": getattr(item, "name", ""),
                "market": getattr(item, "market", "a"),
            }
        candidate = str(value.get("symbol") or "").strip().upper()
        digits = "".join(char for char in candidate if char.isdigit())
        if len(digits) == 6:
            candidate = digits
        market = str(value.get("market") or "a").strip().lower()
        if candidate.isdigit() and len(candidate) == 6 and market in {"a", "cn", "ashare", "sh", "sz"}:
            rows.append({**value, "symbol": candidate, "market": "a"})

    for holding in holdings or []:
        append_row(holding)
    try:
        if MONITOR_CONFIG_PATH.exists():
            config = json.loads(MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
            for key in ("holdings", "watchlist"):
                for item in config.get(key) or []:
                    append_row(item)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"行为因子补全读取监控配置失败: {type(exc).__name__}: {exc}")

    deduped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        candidate = str(row.get("symbol") or "").upper()
        deduped[candidate] = {**deduped.get(candidate, {}), **row}
    return list(deduped.values())


def _resolve_a_share_behavioral_assessment(
    symbol: str,
    *,
    name: str = "",
    provided: Optional[Dict[str, Any]] = None,
    holdings: Optional[List[Any]] = None,
    target_history: Any = None,
) -> Any:
    """Repair a missing factor from the full local A-share cross-section."""
    from core.ashare_behavioral_factor import (
        AShareBehavioralAssessment,
        assess_single_behavioral_history,
    )

    assessment = AShareBehavioralAssessment.from_mapping(provided or {})
    if assessment is not None and not assessment.low_confidence:
        return assessment
    try:
        from jobs.market_monitor_quotes import build_behavioral_factor_context

        context = build_behavioral_factor_context(
            _behavioral_universe_rows(symbol, name=name, holdings=holdings),
        )
        repaired = AShareBehavioralAssessment.from_mapping(context.get(symbol.upper()) or {})
        if repaired is not None and not repaired.low_confidence:
            return repaired
        if repaired is not None:
            assessment = repaired
    except Exception as exc:  # noqa: BLE001
        log.warning(f"A股行为因子横截面补全失败 {symbol}: {type(exc).__name__}: {exc}")

    if assessment is not None:
        return assessment
    if target_history is not None and not getattr(target_history, "empty", True):
        return assess_single_behavioral_history(symbol, target_history)
    return AShareBehavioralAssessment(
        symbol=symbol.upper(),
        low_confidence=True,
        reason="behavioral_cross_section_or_history_unavailable",
    )


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


def _account_ledger_db_path() -> Path:
    from db.account_ledger import DB_PATH

    return Path(DB_PATH)


def _close_account_ledger() -> None:
    global _account_ledger
    if _account_ledger is not None:
        try:
            _account_ledger.close()
        except Exception:
            pass
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


def _validate_monitor_config(config: Dict[str, Any], *, force: bool = False) -> None:
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="Monitor config must be a JSON object")
    holdings = config.get("holdings", [])
    watchlist = config.get("watchlist", [])
    if not isinstance(holdings, list) or not isinstance(watchlist, list):
        raise HTTPException(status_code=400, detail="holdings and watchlist must be arrays")
    if not force and not holdings and not watchlist:
        raise HTTPException(
            status_code=400,
            detail="Refusing an empty monitor config; retry with force=true to clear all targets",
        )
    for item in [*holdings, *watchlist]:
        if not isinstance(item, dict) or not str(item.get("symbol") or "").strip():
            raise HTTPException(status_code=400, detail="Every monitor target must contain symbol")
    for key in ("cash", "total_assets"):
        if key in config:
            try:
                if float(config[key]) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{key} must be a non-negative number")


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


@app.get("/api/accounts/real/decisions")
async def list_real_account_decisions(
    limit: int = Query(200, ge=1, le=1000),
) -> Dict[str, Any]:
    rows = _get_account_ledger().list_decisions(account="real", limit=limit)
    return {"count": len(rows), "rows": rows}


@app.post("/api/accounts/real/decisions/{decision_id}/response")
async def respond_to_real_account_decision(
    decision_id: str,
    body: DecisionResponseRequest = Body(...),
) -> Dict[str, Any]:
    updated = _get_account_ledger().record_decision_response(
        decision_id,
        accepted=body.accepted,
        reason=body.reason or "",
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Real-account decision not found")
    return {"ok": True, "decision_id": decision_id, "accepted": body.accepted}


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
        idempotency_key = body.idempotency_key
        if body.decision_id and not idempotency_key:
            idempotency_key = f"api-user:{body.decision_id}:{body.direction}:{body.units:g}"
        trade = _get_account_ledger().apply_user_trade(
            symbol=body.symbol,
            direction=body.direction,
            units=body.units,
            price=body.price,
            trade_date=body.trade_date,
            note=body.note or "",
            decision_id=body.decision_id,
            idempotency_key=idempotency_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "trade": trade.__dict__}


@app.post("/api/accounts/real/cash")
async def correct_real_account_cash(body: CashCorrectionRequest = Body(...)) -> Dict[str, Any]:
    try:
        from db.account_ledger import REAL_ACCOUNT
        _get_account_ledger().correct_cash(
            account=REAL_ACCOUNT,
            cash=body.cash,
            t2_pending=body.t2_pending_cash,
        )
        return {"ok": True, "message": "可用现金/待交收现金修正成功"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to correct cash: {str(e)}")



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
        behavioral_assessment = None
        if (req.market or "a").lower() == "a":
            behavioral_assessment = _resolve_a_share_behavioral_assessment(
                req.symbol,
                name=req.name,
                provided=req.behavioral_factor,
                holdings=req.holdings,
                target_history=df_2y,
            )
            market_data += behavioral_assessment.audit_text()
        from core.buy_signal_miner import mine_historical_buy_signals
        buy_signal_backtest = mine_historical_buy_signals(req.symbol, df_2y)

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

        # 嵌套评估函数，供真实账户和影子账户重用
        def _evaluate_portfolio(
            cash_val: float,
            available_cash_val: float,
            t2_pending_val: float,
            position_pct_val: float,
            cost_val: float,
            holdings_list: List[Holding],
        ) -> Dict[str, Any]:
            temp_req = CommitteeRequest(
                symbol=req.symbol,
                name=req.name,
                market=req.market,
                sector=req.sector,
                industry=req.industry,
                position_pct=position_pct_val,
                target_position_pct=req.target_position_pct,
                cost=cost_val,
                current_price=current_price,
                total_assets=req.total_assets,
                cash=cash_val,
                holdings=holdings_list,
                risk_preference=req.risk_preference,
                max_debate_rounds=req.max_debate_rounds,
                min_lot_size=req.min_lot_size,
                t_plus_1=req.t_plus_1,
                available_cash=available_cash_val,
                t2_pending_cash=t2_pending_val,
                news_brief=req.news_brief,
                fundamentals=req.fundamentals,
                optimizer_review_enabled=req.optimizer_review_enabled,
                behavioral_factor=req.behavioral_factor,
            )
            p_summary = _build_portfolio_summary(temp_req)

            p_summary += f"\n\n### 基本面数学模型锚点\n{fundamental_brief}"

            from core.committee import run_wealth_context_view
            w_context = run_wealth_context_view(None, cash_val)

            from core.committee import run_committee, parse_cio_memo

            res = run_committee(
                asset=asset,
                market_data=market_data,
                macro_view=macro_view,
                portfolio_summary=p_summary,
                prior_insights="",
                regime_brief=regime_brief,
                wealth_context_view=w_context,
                current_price=current_price or None,
                persist_to_memory=False,
                max_debate_rounds=min(req.max_debate_rounds, 4),
            )

            report = res.get("report")
            if report is None:
                return {"success": False, "error": f"委员会返回空 report: {res.get('error', 'unknown')}"}

            c_memo = report.cio_memo or ""
            parsed = parse_cio_memo(c_memo, current_price=current_price)

            atr_pct = metrics.get("atr_pct") if metrics else None
            if atr_pct and current_price and current_price > 0:
                atr_amount = current_price * atr_pct / 100
                atr_sl = current_price - 2 * atr_amount
                atr_tp = current_price + 3 * atr_amount
                c_memo += (
                    f"\n[ATR] ATR={atr_amount:.2f}({atr_pct:.1f}%) "
                    f"止损2N={atr_sl:.2f} 止盈3N={atr_tp:.2f}"
                )

            from core.decision_optimizer import optimize_committee_decision
            from core.decision_synthesis import synthesize_decision
            from core.entry_exit_points import compute_entry_exit_points
            from core.right_side_trend_gate import evaluate_right_side_trend_gate

            opt = optimize_committee_decision(
                parsed=parsed,
                metrics=metrics,
                symbol=req.symbol,
                regime_brief=regime_brief,
                current_price=current_price,
                total_assets=req.total_assets,
                available_cash=available_cash_val if available_cash_val > 0 else cash_val,
                position_pct=position_pct_val,
                min_lot_size=req.min_lot_size,
                bl_anchor_target_pct=req.target_position_pct,
                market=req.market,
                risk_preference=req.risk_preference,
                regime_probability=regime_probability,
                conditional_return_stats=conditional_return_stats,
                fundamental_assessment=fundamental_assessment,
                position_exit_policy=None,
                behavioral_assessment=behavioral_assessment,
                require_a_share_behavioral=True,
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

            right_side_gate = evaluate_right_side_trend_gate(
                metrics=metrics,
                regime_brief=regime_brief,
                optimizer_expected_return_pct=opt.expected_return_pct,
                entry_exit_points=entry_exit_plan.as_dict(),
                conditional_return_stats=conditional_return_stats,
                quant_view=report.quant_view or "",
                risk_view=report.risk_view or "",
                cio_memo=c_memo,
                market=req.market,
                is_holding=position_pct_val > 0,
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
                        position_exit_policy_audit="",
                        regime_brief=regime_brief,
                        fundamental_brief=fundamental_brief,
                    )
                except Exception as e:
                    optimizer_review = (
                        "[WORKER_UNAVAILABLE] "
                        f"reason=optimizer_review_failed exc_type={type(e).__name__}"
                    )

            has_loss = (
                position_pct_val > 0
                and cost_val > 0
                and current_price
                and current_price < cost_val
            )
            if has_loss and opt.verdict in ("SELL", "TRIM"):
                c_memo += (
                    f"\n[FLOATING_LOSS_PROTECT] 持仓浮亏({(current_price/cost_val-1)*100:.1f}%)，"
                    f"优化器{opt.verdict}→保留LLM裁决，需人工判断"
                    f"\n[OPTIMAL_DECISION]"
                    f"\nside=hold verdict=HOLD lots=0 alloc_cny=0"
                    f"\nreason=floating_loss_protected_optimizer_sell_blocked"
                    f"\nblack_litterman_anchor_target={position_pct_val:.1f}%"
                )
                parsed["alloc_cny"] = 0
                parsed["target_position_pct"] = max(5.0, min(position_pct_val, 35.0))
            else:
                right_side_blocked = (
                    position_pct_val <= 0
                    and opt.verdict in {"BUY", "ACCUMULATE"}
                    and not right_side_gate.allow
                )
                if right_side_blocked:
                    c_memo += (
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
                        c_memo += (
                            f"\n[OPTIMIZER_OVERRIDE] LLM={parsed.get('verdict')} "
                            f"alloc={parsed.get('alloc_cny', 0)} -> {opt.verdict} alloc={opt.alloc_cny}"
                        )
                    parsed["verdict"] = opt.verdict
                    parsed["confidence"] = opt.confidence
                    parsed["alloc_cny"] = opt.alloc_cny
            c_memo += opt_audit_text
            c_memo += entry_exit_audit_text
            c_memo += right_side_gate_text
            if optimizer_review:
                c_memo += f"\n\n[OPTIMIZER_LLM_REVIEW]\n{optimizer_review}"
            decision_synthesis = synthesize_decision(
                symbol=req.symbol,
                name=req.name,
                optimizer=opt,
                parsed=parsed,
                entry_exit_points=entry_exit_plan.as_dict(),
                right_side_gate=right_side_gate.as_dict(),
                position_exit_policy={},
                optimizer_review=optimizer_review,
                current_price=current_price,
                is_holding=position_pct_val > 0,
            )
            c_memo += decision_synthesis.audit_text()

            return {
                "success": True,
                "verdict": parsed.get("verdict", "UNCLEAR"),
                "confidence": parsed.get("confidence", 0.0),
                "dominant_view": parsed.get("dominant_view", "tie"),
                "suggested_alloc_cny": parsed.get("alloc_cny", 0),
                "cio_memo": c_memo,
                "report": report,
                "entry_exit_plan": entry_exit_plan,
                "position_exit_policy": {},
                "right_side_gate": right_side_gate,
                "optimizer_review": optimizer_review,
                "decision_synthesis": decision_synthesis,
                "buy_signal_backtest": buy_signal_backtest,
                "behavioral_factor": behavioral_assessment,
            }

        # 6. 跑真实账户（Real）的评估
        log.info(f"启动真实账户委员会辩论 (max rounds={req.max_debate_rounds})...")
        real_res = _evaluate_portfolio(
            cash_val=req.cash,
            available_cash_val=req.available_cash,
            t2_pending_val=req.t2_pending_cash,
            position_pct_val=req.position_pct,
            cost_val=req.cost,
            holdings_list=req.holdings,
        )
        if not real_res.get("success"):
            return CommitteeResponse(
                success=False,
                symbol=req.symbol,
                name=req.name,
                error=real_res.get("error", "unknown"),
                elapsed_sec=(datetime.now() - t0).total_seconds(),
            )

        # 7. 跑影子账户（Shadow）的评估（若传入相关数据）
        shadow_result_dict = None
        if req.shadow_cash > 0 or req.shadow_position_pct > 0 or len(req.shadow_holdings) > 0 or req.shadow_available_cash > 0:
            log.info("启动影子账户（Committee）的独立委员会辩论...")
            shadow_res = _evaluate_portfolio(
                cash_val=req.shadow_cash,
                available_cash_val=req.shadow_available_cash,
                t2_pending_val=req.shadow_t2_pending_cash,
                position_pct_val=req.shadow_position_pct,
                cost_val=req.shadow_cost,
                holdings_list=req.shadow_holdings,
            )
            if shadow_res.get("success"):
                shadow_report = shadow_res["report"]
                shadow_result_dict = {
                    "success": True,
                    "symbol": req.symbol,
                    "name": req.name or req.symbol,
                    "market": req.market,
                    "verdict": shadow_res["verdict"],
                    "confidence": shadow_res["confidence"],
                    "suggested_alloc_cny": shadow_res["suggested_alloc_cny"],
                    "cio_memo": shadow_res["cio_memo"],
                    "macro_view": shadow_report.macro_view or "",
                    "quant_view": shadow_report.quant_view or "",
                    "risk_view": shadow_report.risk_view or "",
                    "entry_exit_points": shadow_res["entry_exit_plan"].as_dict(),
                    "position_exit_policy": {},
                    "right_side_trend_gate": shadow_res["right_side_gate"].as_dict(),
                    "optimizer_review": shadow_res["optimizer_review"],
                    "decision_synthesis": shadow_res["decision_synthesis"].as_dict(),
                    "buy_signal_backtest": shadow_res["buy_signal_backtest"].as_dict(),
                    "behavioral_factor": (
                        shadow_res["behavioral_factor"].as_dict()
                        if shadow_res.get("behavioral_factor") is not None else {}
                    ),
                }
            else:
                log.warning(f"影子账户委员会评估失败: {shadow_res.get('error')}")

        # 8. 缓存真实账户（Real）结果到本地二进制 pickle
        real_report = real_res["report"]
        _save_committee_cache(
            symbol=req.symbol,
            name=req.name or req.symbol,
            current_price=current_price or 0,
            verdict=real_res["verdict"],
            confidence=real_res["confidence"],
            suggested_alloc=real_res["suggested_alloc_cny"],
            quant_signal=real_report.quant_view[:200] if real_report.quant_view else "",
            regime=regime_brief[:500],
            fundamental_model=fundamental_assessment.model_key,
            fundamental_score=fundamental_assessment.score,
            entry_exit_points=real_res["entry_exit_plan"].as_dict(),
            position_exit_policy={},
            right_side_trend_gate=real_res["right_side_gate"].as_dict(),
            optimizer_review=real_res["optimizer_review"][:500],
            cio_note=real_res["cio_memo"][:500],
        )

        elapsed = (datetime.now() - t0).total_seconds()
        log.info(f"委员会完成: verdict={real_res['verdict']} confidence={real_res['confidence']:.2f} elapsed={elapsed:.1f}s")

        return CommitteeResponse(
            success=True,
            symbol=req.symbol,
            name=req.name,
            market=req.market,
            verdict=real_res["verdict"],
            confidence=real_res["confidence"],
            dominant_view=real_res["dominant_view"],
            suggested_alloc_cny=real_res["suggested_alloc_cny"],
            cio_memo=real_res["cio_memo"],
            macro_view=real_report.macro_view or "",
            quant_view=real_report.quant_view or "",
            risk_view=real_report.risk_view or "",
            quant_adjusted=real_report.quant_adjusted or "",
            risk_adjusted=real_report.risk_adjusted or "",
            market_data=market_data[:2000],
            regime=regime_brief[:500],
            fundamental_model=fundamental_assessment.model_key,
            fundamental_score=fundamental_assessment.score,
            fundamental_coverage=fundamental_assessment.coverage,
            fundamental_anchor_multiplier=fundamental_assessment.anchor_multiplier,
            entry_exit_points=real_res["entry_exit_plan"].as_dict(),
            position_exit_policy={},
            right_side_trend_gate=real_res["right_side_gate"].as_dict(),
            optimizer_review=real_res["optimizer_review"],
            decision_synthesis=real_res["decision_synthesis"].as_dict(),
            buy_signal_backtest=real_res["buy_signal_backtest"].as_dict(),
            behavioral_factor=(
                real_res["behavioral_factor"].as_dict()
                if real_res.get("behavioral_factor") is not None else {}
            ),
            elapsed_sec=round(elapsed, 1),
            shadow_result=shadow_result_dict,
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
    res = run_committee_direct(req)
    res.shadow_result = None
    return res


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
async def import_monitor_config(
    background_tasks: BackgroundTasks,
    config: Dict[str, Any] = Body(...),
    force: bool = Query(False),
):
    """导入/覆盖仓位及自选标的配置文件 (market_monitor_config.json)

    写文件立即返回（~ms），账本重建和快照清理在后台执行，避免客户端超时。
    """
    try:
        from utils.safe_persistence import backup_and_atomic_write_json

        _validate_monitor_config(config, force=force)
        backup = backup_and_atomic_write_json(
            MONITOR_CONFIG_PATH,
            config,
            reason="api-monitor-config-import",
        )

        def _refresh_ledger_and_cleanup():
            try:
                _ledger = _get_account_ledger()
                _ledger.ensure_initialized(config)
                log.info("Ledger refreshed from imported monitor config without destructive reset")
            except Exception as le:
                log.warning(f"Failed to refresh ledger after import: {le}")
            try:
                from scripts.monitor_window_constants import DEFAULT_SNAPSHOT
                if DEFAULT_SNAPSHOT.exists():
                    DEFAULT_SNAPSHOT.unlink()
                    log.info("Stale snapshot file deleted after config import")
            except Exception as se:
                log.warning(f"Failed to delete stale snapshot file: {se}")

        background_tasks.add_task(_refresh_ledger_and_cleanup)
        return {
            "ok": True,
            "message": "Monitor configuration saved, refreshing ledger in background",
            "backup_id": backup["backup_id"],
        }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
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


def _prefetch_symbol_data(symbol: str, market: str = "a") -> None:
    """后台预拉标的历史行情数据，写入 SQLite 缓存供 MA/ATR 等技术指标计算。

    get_history_data 内部有缓存判断（3 天内不重复拉取），所以重复调用开销很小。
    """
    try:
        from utils.market_data_provider import get_history_data

        history_symbol = str(symbol or "").strip().upper()
        market_key = str(market or "").strip().lower()
        if market_key in {"hk", "h", "hongkong"} and history_symbol.isdigit():
            history_symbol = history_symbol.zfill(5)

        df = get_history_data(history_symbol, "2y")
        rows = 0 if df is None or df.empty else len(df)
        log.info(f"Prefetched history for {history_symbol} ({market_key or market}): {rows} bars")
    except Exception as e:  # noqa: BLE001
        log.warning(f"Prefetch history failed for {symbol}: {e}")



def _refresh_monitor_snapshot(reason: str = "holding_changed") -> None:
    """后台重新生成监控快照（含实时价格 + 技术指标），不调用 DeepSeek/委员会。

    构建新快照后原子替换旧文件：
    1. 从 market_monitor_config.json 读持仓
    2. 调 fetch_sina_prices 拉实时价
    3. 调 _compute_tech_from_local_history 从 SQLite 算 MA20/MA120/ATR_PCT
    """
    try:
        from scripts.monitor_window_services import _load_config_snapshot, _write_snapshot
        from scripts.monitor_window_constants import DEFAULT_SNAPSHOT

        snapshot = _load_config_snapshot(reason, source_path=DEFAULT_SNAPSHOT)
        _write_snapshot(DEFAULT_SNAPSHOT, snapshot)
        rows = snapshot.get("rows", [])
        log.info(f"Monitor snapshot refreshed ({reason}): {len(rows)} symbols")
    except Exception as e:  # noqa: BLE001
        log.warning(f"Monitor snapshot refresh failed ({reason}): {e}")


def _prefetch_and_refresh(symbol: str, market: str = "a") -> None:
    """新增标的：先拉历史数据写入 DB，再刷新快照生成完整技术指标。"""
    _prefetch_symbol_data(symbol, market)
    _refresh_monitor_snapshot(f"added:{symbol}")


@app.post("/api/holdings")
async def add_holding_api(background_tasks: BackgroundTasks, body: Dict[str, Any] = Body(...)):
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
        
        _ledger = _get_account_ledger()
        from db.account_ledger import REAL_ACCOUNT
        
        # Check if already exists in ledger holdings
        existing_holdings = _ledger.list_holdings(REAL_ACCOUNT)
        if any(str(h.get("symbol")).strip().upper() == symbol for h in existing_holdings):
            return {"ok": True, "message": "已在持仓或自选列表中", "duplicate": True}
        
        actual_units = 0.0 if is_tracking_only else units
        actual_cost = 0.0 if is_tracking_only else cost
        
        success = _ledger.add_holding(
            account=REAL_ACCOUNT,
            symbol=symbol,
            name=name,
            market=market,
            sector=sector,
            industry=sector,
            units=actual_units,
            cost=actual_cost,
            min_lot_size=100
        )
        if not success:
            raise HTTPException(status_code=500, detail="Failed to add holding to ledger")

        # 标的变化：后台拉历史数据 + 刷新快照（含 MA/ATR 等技术指标）
        background_tasks.add_task(_prefetch_and_refresh, symbol, market)

        return {"ok": True, "message": "已加入关注列表" if is_tracking_only else "已加入持仓列表"}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to add holding: {str(e)}")


@app.put("/api/holdings/{symbol}")
async def update_holding_api(background_tasks: BackgroundTasks, symbol: str, body: Dict[str, Any] = Body(...)):
    """更新持仓或自选状态。如果 is_tracking_only=True 则将标的移入/保留在 watchlist，否则移入/保留在 holdings。"""
    try:
        symbol = symbol.strip().upper()
        units = float(body.get("units") or 0.0)
        cost = float(body.get("avg_cost") or body.get("cost") or 0.0)
        is_tracking_only = bool(body.get("is_tracking_only", True))
        
        _ledger = _get_account_ledger()
        from db.account_ledger import REAL_ACCOUNT
        
        # Check if exists
        existing_holdings = _ledger.list_holdings(REAL_ACCOUNT)
        if not any(str(h.get("symbol")).strip().upper() == symbol for h in existing_holdings):
            raise HTTPException(status_code=404, detail=f"未找到标的: {symbol}")
            
        success = _ledger.update_holding(
            account=REAL_ACCOUNT,
            symbol=symbol,
            units=units,
            cost=cost,
            is_tracking_only=is_tracking_only
        )
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update holding in ledger")

        # 标的变化：后台刷新快照
        background_tasks.add_task(_refresh_monitor_snapshot, f"updated:{symbol}")

        return {"ok": True, "message": "持仓数据更新成功"}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to update holding: {str(e)}")


@app.delete("/api/holdings/{symbol}")
async def delete_holding_api(background_tasks: BackgroundTasks, symbol: str):
    """删除持仓或自选标的。"""
    try:
        symbol = symbol.strip().upper()
        _ledger = _get_account_ledger()
        from db.account_ledger import REAL_ACCOUNT
        
        # Find item in ledger holdings
        existing_holdings = _ledger.list_holdings(REAL_ACCOUNT)
        target = next((h for h in existing_holdings if str(h.get("symbol")).strip().upper() == symbol), None)
        if not target:
            raise HTTPException(status_code=404, detail=f"标的 {symbol} 不存在")
            
        # Check if units > 0 (avoid deleting real assets with units)
        if float(target.get("units", 0) or 0) > 0:
            raise HTTPException(status_code=400, detail="持仓股数大于0，不能直接删除。请先平仓或设股数为0")
            
        success = _ledger.delete_holding(REAL_ACCOUNT, symbol)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete holding from ledger")

        # 标的变化：后台刷新快照（移除已删除标的）
        background_tasks.add_task(_refresh_monitor_snapshot, f"deleted:{symbol}")

        return {"ok": True, "message": "已删除关注"}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to delete holding: {str(e)}")


@app.post("/api/config/exit_params")
async def import_exit_params(params: Dict[str, Any] = Body(...), force: bool = Query(False)):
    """Deprecated compatibility endpoint; position discipline is retired."""
    _ = (params, force)
    return {"ok": True, "message": "Position exit parameters are retired and were ignored"}


@app.get("/api/config/exit_params")
async def get_exit_params():
    """Deprecated compatibility endpoint; always return an empty object."""
    return {}


@app.post("/api/config/sector_cache")
async def import_sector_cache(cache: Dict[str, Any] = Body(...), force: bool = Query(False)):
    """导入/覆盖板块缓存文件 (sector_cache.json)"""
    try:
        cache_path = _PROJECT_ROOT / "data" / "sector_cache.json"
        if not force and not cache:
            raise HTTPException(status_code=400, detail="Refusing empty sector cache; retry with force=true")
        from utils.safe_persistence import backup_and_atomic_write_json

        backup = backup_and_atomic_write_json(cache_path, cache, reason="api-sector-cache-import")
        return {
            "ok": True,
            "message": "Sector cache file imported successfully",
            "backup_id": backup["backup_id"],
        }
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to save sector cache: {str(e)}")


@app.get("/api/config/sector_cache")
async def get_sector_cache():
    """获取板块缓存文件内容"""
    try:
        cache_path = _PROJECT_ROOT / "data" / "sector_cache.json"
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="Sector cache file not found")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to read sector cache: {str(e)}")


@app.post("/api/config/env_policies")
async def import_env_policies(body: Dict[str, Any] = Body(...), force: bool = Query(False)):
    """Deprecated compatibility endpoint; sector exit policies are retired."""
    _ = (body, force)
    return {"ok": True, "message": "Sector exit policies are retired and were ignored"}


@app.get("/api/config/env_policies")
async def get_env_policies():
    """Deprecated compatibility endpoint; always return an empty policy."""
    return {"policies": ""}


@app.post("/api/config/account_ledger")
async def import_account_ledger(file: UploadFile = File(...), force: bool = Query(False)):
    """上传并覆盖双账户账本文件 (accounts.db)"""
    try:
        db_path = _account_ledger_db_path()
        payload = await file.read(64 * 1024 * 1024 + 1)
        if len(payload) > 64 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Account ledger upload exceeds 64 MiB")
        _close_account_ledger()
        from utils.safe_persistence import restore_sqlite_bytes

        restore = restore_sqlite_bytes(
            db_path,
            payload,
            reason="api-account-ledger-import",
            required_tables=("accounts", "holdings", "trades", "daily_pnl"),
            nonempty_tables=("accounts", "holdings"),
            force=force,
        )

        ledger = _get_account_ledger()
        try:
            ledger.sync_to_config_and_snapshot()
        except Exception as sync_err:
            log.warning(f"Failed to sync config/snapshot after account ledger import: {sync_err}")
        return {
            "ok": True,
            "message": "Account ledger database imported successfully",
            "backup_id": restore["backup_id"],
            "sqlite_user_version": restore["restored"]["user_version"],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to save account ledger: {str(e)}")


@app.get("/api/config/account_ledger")
async def download_account_ledger():
    """下载双账户账本文件"""
    try:
        _get_account_ledger()
        db_path = _account_ledger_db_path()
        if db_path.exists():
            from utils.safe_persistence import create_sqlite_export

            export_path = create_sqlite_export(
                db_path,
                required_tables=("accounts", "holdings", "trades", "daily_pnl"),
            )
            return FileResponse(
                path=str(export_path),
                filename="accounts.db",
                media_type="application/octet-stream",
                background=BackgroundTask(lambda: export_path.unlink(missing_ok=True)),
            )
        raise HTTPException(status_code=404, detail="Account ledger database file not found")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Failed to download account ledger: {str(e)}")


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


@app.get("/api/stock/behavioral")
async def get_behavioral_assessment_api(symbol: str):
    """获取指定A股的最新行为因子评估，供手机本地委员会使用"""
    symbol = symbol.strip().upper()
    try:
        from utils.market_data_provider import get_history_data
        df_2y = get_history_data(symbol, "2y")
        assessment = _resolve_a_share_behavioral_assessment(
            symbol,
            target_history=df_2y,
        )
        return {
            "success": True,
            "symbol": symbol,
            "assessment": assessment.as_dict(),
            "decidable": not assessment.low_confidence,
        }
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
