"""Deterministic fundamental model selection and scoring.

The model is intentionally data-source agnostic: callers can pass audited
fundamental metrics from config, a financial-data connector, or a manual form.
Missing metrics keep the assessment neutral instead of fabricating a signal.

Model references behind the sector-specific factor choices:
- Financial-statement quality/value scoring:
  Piotroski, J. D. (2000), "Value Investing: The Use of Historical
  Financial Statement Information to Separate Winners from Losers".
  https://doi.org/10.1111/1475-679X.00020
- Profitability / quality factor:
  Novy-Marx, R. (2013), "The Other Side of Value: The Gross Profitability
  Premium". https://doi.org/10.1016/j.jfineco.2013.01.003
- Residual-income model for financials / PB-ROE logic:
  Ohlson, J. A. (1995), "Earnings, Book Values, and Dividends in Equity
  Valuation". https://doi.org/10.1111/j.1911-3846.1995.tb00461.x
- Dividend-discount / yield anchor for regulated utilities:
  Gordon, M. J. (1959), "Dividends, Earnings, and Stock Prices".
  https://doi.org/10.2307/1927792
- Multi-factor equity model background:
  Fama, E. F. and French, K. R. (1993), "Common Risk Factors in the
  Returns on Stocks and Bonds". https://doi.org/10.1016/0304-405X(93)90023-5
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


ScoreFn = Callable[[float], float]


@dataclass(frozen=True)
class FundamentalFactor:
    name: str
    weight: float
    score_fn: ScoreFn


@dataclass(frozen=True)
class FundamentalAssessment:
    symbol: str
    model_key: str
    model_name: str
    sector: str
    industry: str
    score: float
    composite: float
    coverage: float
    anchor_multiplier: float
    expected_return_adjustment_pct: float
    low_confidence: bool
    used_metrics: Dict[str, float]
    factor_scores: Dict[str, float]
    reason: str

    def summary_text(self) -> str:
        conf = "low_coverage" if self.low_confidence else "usable"
        used = ", ".join(
            f"{k}={v:.2f}" for k, v in sorted(self.used_metrics.items())
        ) or "none"
        return (
            f"MODEL: {self.model_key} ({self.model_name})\n"
            f"SECTOR: {self.sector or 'unknown'} INDUSTRY: {self.industry or 'unknown'}\n"
            f"SCORE: {self.score:.1f}/100 COVERAGE: {self.coverage:.0%} CONFIDENCE: {conf}\n"
            f"ANCHOR_MULTIPLIER: {self.anchor_multiplier:.3f}\n"
            f"EXPECTED_RETURN_ADJ_30D: {self.expected_return_adjustment_pct:+.2f}%\n"
            f"USED_METRICS: {used}\n"
            f"REASON: {self.reason}"
        )


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _as_pct(x: float) -> float:
    """Accept either 0.12 or 12 for percentage-like metrics.

    本 codebase 既定惯例是**小数**形式（revenue_growth=0.28 表示 28%、
    fcf_yield=0.04 表示 4%、NIM=0.013 表示 1.3%；见 tests/test_fundamental_model.py），
    所以 |x|<=1.5 的输入按小数 ×100 归一到整数百分比域。阈值 1.5 是因为真实
    百分比指标的小数形式几乎都 <=1.5（150%），而少数天然 >100% 的指标
    （如 provision_coverage 280%）按整数惯例传入、落在 else 分支原样返回。
    """
    return x * 100.0 if abs(x) <= 1.5 else x


def _higher_better(weak: float, strong: float, *, pct: bool = False) -> ScoreFn:
    def _score(x: float) -> float:
        v = _as_pct(x) if pct else x
        return _clamp((v - weak) / (strong - weak) * 2.0 - 1.0, -1.0, 1.0)
    return _score


def _lower_better(strong: float, weak: float, *, pct: bool = False) -> ScoreFn:
    def _score(x: float) -> float:
        v = _as_pct(x) if pct else x
        return _clamp((weak - v) / (weak - strong) * 2.0 - 1.0, -1.0, 1.0)
    return _score


def _near_target(target: float, tolerance: float, bad: float, *, pct: bool = False) -> ScoreFn:
    def _score(x: float) -> float:
        v = _as_pct(x) if pct else x
        dist = abs(v - target)
        if dist <= tolerance:
            return 1.0
        return _clamp(1.0 - (dist - tolerance) / max(bad - tolerance, 1e-9) * 2.0, -1.0, 1.0)
    return _score


def _valuation_pe() -> ScoreFn:
    return _lower_better(12.0, 55.0)


def _valuation_pb() -> ScoreFn:
    return _lower_better(1.0, 8.0)


def _valuation_ps_growth() -> ScoreFn:
    return _lower_better(2.0, 18.0)


def _factors(items: Iterable[Tuple[str, float, ScoreFn]]) -> Tuple[FundamentalFactor, ...]:
    return tuple(FundamentalFactor(*item) for item in items)


MODEL_FACTORS: Dict[str, Tuple[FundamentalFactor, ...]] = {
    # Damodaran-style cash-yield / dividend stability model.
    "regulated_yield": _factors([
        ("dividend_yield_pct", 0.22, _higher_better(2.0, 6.0, pct=True)),
        ("payout_ratio_pct", 0.12, _near_target(65.0, 20.0, 55.0, pct=True)),
        ("ocf_to_net_income", 0.16, _higher_better(0.8, 1.4)),
        ("debt_to_assets_pct", 0.15, _lower_better(35.0, 70.0, pct=True)),
        ("roe_pct", 0.13, _higher_better(6.0, 14.0, pct=True)),
        ("pe_ttm", 0.12, _lower_better(10.0, 30.0)),
        ("volatility_earnings_pct", 0.10, _lower_better(5.0, 35.0, pct=True)),
    ]),
    # Technology / internet / semiconductor: growth quality plus valuation discipline.
    "growth_innovation": _factors([
        ("revenue_growth_pct", 0.18, _higher_better(5.0, 30.0, pct=True)),
        ("gross_margin_pct", 0.14, _higher_better(25.0, 60.0, pct=True)),
        ("operating_margin_pct", 0.13, _higher_better(5.0, 25.0, pct=True)),
        ("net_margin_pct", 0.08, _higher_better(5.0, 25.0, pct=True)),
        ("rd_to_revenue_pct", 0.12, _higher_better(3.0, 16.0, pct=True)),
        ("roe_pct", 0.08, _higher_better(6.0, 22.0, pct=True)),
        ("roic_pct", 0.14, _higher_better(6.0, 22.0, pct=True)),
        ("fcf_yield_pct", 0.12, _higher_better(0.0, 6.0, pct=True)),
        ("pe_ttm", 0.06, _valuation_pe()),
        ("ps", 0.09, _valuation_ps_growth()),
        ("peg", 0.08, _lower_better(0.8, 3.0)),
        ("net_debt_to_ebitda", 0.10, _lower_better(0.0, 3.5)),
    ]),
    # Banks and insurers: residual-income logic via ROE, PB, capital and credit risk.
    "financial_residual_income": _factors([
        ("roe_pct", 0.22, _higher_better(7.0, 16.0, pct=True)),
        ("pb", 0.16, _lower_better(0.6, 2.0)),
        ("npl_ratio_pct", 0.16, _lower_better(0.8, 3.0, pct=True)),
        ("provision_coverage_pct", 0.14, _higher_better(120.0, 280.0, pct=True)),
        ("cet1_ratio_pct", 0.12, _higher_better(9.0, 14.0, pct=True)),
        ("net_interest_margin_pct", 0.10, _higher_better(1.3, 2.6, pct=True)),
        ("cost_income_ratio_pct", 0.10, _lower_better(25.0, 55.0, pct=True)),
    ]),
    # Cyclical industrials / manufacturing: Piotroski-like quality, balance sheet, cash flow.
    "industrial_quality_value": _factors([
        ("roe_pct", 0.16, _higher_better(6.0, 18.0, pct=True)),
        ("roic_pct", 0.14, _higher_better(5.0, 18.0, pct=True)),
        ("revenue_growth_pct", 0.12, _higher_better(-5.0, 20.0, pct=True)),
        ("operating_margin_pct", 0.12, _higher_better(4.0, 18.0, pct=True)),
        ("ocf_to_net_income", 0.13, _higher_better(0.7, 1.4)),
        ("debt_to_equity", 0.12, _lower_better(0.3, 2.0)),
        ("asset_turnover", 0.08, _higher_better(0.3, 1.2)),
        ("pe_ttm", 0.08, _valuation_pe()),
        ("pb", 0.05, _valuation_pb()),
    ]),
    # Consumer / healthcare compounders: quality and reinvestment runway.
    "quality_compounder": _factors([
        ("roe_pct", 0.17, _higher_better(10.0, 25.0, pct=True)),
        ("roic_pct", 0.17, _higher_better(8.0, 25.0, pct=True)),
        ("gross_margin_pct", 0.13, _higher_better(30.0, 70.0, pct=True)),
        ("revenue_growth_pct", 0.13, _higher_better(3.0, 22.0, pct=True)),
        ("profit_growth_pct", 0.13, _higher_better(0.0, 25.0, pct=True)),
        ("fcf_yield_pct", 0.11, _higher_better(1.0, 7.0, pct=True)),
        ("net_debt_to_ebitda", 0.08, _lower_better(0.0, 3.0)),
        ("pe_ttm", 0.08, _valuation_pe()),
    ]),
    # Fallback multi-factor model when the sector is unknown.
    "generic_multifactor": _factors([
        ("roe_pct", 0.16, _higher_better(6.0, 20.0, pct=True)),
        ("revenue_growth_pct", 0.13, _higher_better(-5.0, 25.0, pct=True)),
        ("profit_growth_pct", 0.12, _higher_better(-8.0, 25.0, pct=True)),
        ("fcf_yield_pct", 0.12, _higher_better(0.0, 7.0, pct=True)),
        ("debt_to_equity", 0.12, _lower_better(0.3, 2.2)),
        ("operating_margin_pct", 0.11, _higher_better(4.0, 22.0, pct=True)),
        ("pe_ttm", 0.10, _valuation_pe()),
        ("pb", 0.08, _valuation_pb()),
        ("current_ratio", 0.06, _higher_better(0.8, 2.0)),
    ]),
}


MODEL_NAMES = {
    "regulated_yield": "Dividend discount / cash-yield stability",
    "growth_innovation": "Growth quality with R&D and valuation discipline",
    "financial_residual_income": "Residual-income financial-sector model",
    "industrial_quality_value": "Piotroski-style industrial quality-value",
    "quality_compounder": "Quality compounder multi-factor",
    "generic_multifactor": "Generic quality-growth-value multi-factor",
}


KNOWN_SYMBOL_MODELS = {
    "600900": "regulated_yield",
    "00700": "growth_innovation",
    "09988": "growth_innovation",
    "601138": "growth_innovation",
    "600487": "industrial_quality_value",
    "300476": "growth_innovation",
    "600183": "growth_innovation",
    "601179": "industrial_quality_value",
    "002185": "growth_innovation",
    "301377": "industrial_quality_value",
    "600580": "industrial_quality_value",
    "688099": "growth_innovation",
    "000063": "growth_innovation",
    "603308": "industrial_quality_value",
    "688017": "industrial_quality_value",
}


KEYWORD_MODELS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("financial_residual_income", (
        "bank", "insurance", "broker", "securities", "finance",
        "银行", "保险", "券商", "证券", "金融",
    )),
    ("regulated_yield", (
        "utility", "utilities", "power", "electricity", "grid", "water",
        "toll", "telecom operator", "公用事业", "电力", "水电", "运营商", "高速",
    )),
    ("growth_innovation", (
        "semiconductor", "software", "internet", "cloud", "ai", "chip",
        "electronics", "pcb", "通信设备", "半导体", "芯片", "互联网", "软件",
        "电子", "算力", "光模块", "服务器", "通信", "印制电路",
    )),
    ("quality_compounder", (
        "consumer", "healthcare", "medical", "medicine", "pharma",
        "food", "beverage", "消费", "医药", "医疗", "食品", "饮料",
    )),
    ("industrial_quality_value", (
        "industrial", "machinery", "equipment", "manufacturing", "materials",
        "robot", "auto parts", "工业", "机械", "设备", "制造", "材料", "机器人",
        "电机", "电气",
    )),
)


ALIASES: Dict[str, Tuple[str, ...]] = {
    "roe_pct": ("roe", "roe_pct", "return_on_equity"),
    "roic_pct": ("roic", "roic_pct", "return_on_invested_capital"),
    "gross_margin_pct": ("gross_margin", "gross_margin_pct"),
    "operating_margin_pct": ("operating_margin", "operating_margin_pct", "op_margin"),
    "net_margin_pct": ("net_margin", "net_margin_pct"),
    "revenue_growth_pct": ("revenue_growth", "revenue_growth_pct", "sales_growth"),
    "profit_growth_pct": ("profit_growth", "profit_growth_pct", "earnings_growth", "eps_growth"),
    "fcf_yield_pct": ("fcf_yield", "fcf_yield_pct", "free_cash_flow_yield"),
    "dividend_yield_pct": ("dividend_yield", "dividend_yield_pct", "dy"),
    "payout_ratio_pct": ("payout_ratio", "payout_ratio_pct"),
    "debt_to_equity": ("debt_to_equity", "de_ratio"),
    "debt_to_assets_pct": ("debt_to_assets", "debt_to_assets_pct"),
    "net_debt_to_ebitda": ("net_debt_to_ebitda", "ndebt_ebitda"),
    "current_ratio": ("current_ratio",),
    "pe_ttm": ("pe", "pe_ttm", "pettm"),
    "pb": ("pb", "pb_ratio"),
    "ps": ("ps", "ps_ratio"),
    "peg": ("peg", "peg_ratio"),
    "ev_ebitda": ("ev_ebitda", "ev_to_ebitda"),
    "rd_to_revenue_pct": ("rd_to_revenue", "rd_to_revenue_pct", "r_and_d_to_revenue"),
    "asset_turnover": ("asset_turnover",),
    "inventory_turnover": ("inventory_turnover",),
    "ocf_to_net_income": ("ocf_to_net_income", "operating_cash_flow_to_net_income"),
    "capex_to_revenue_pct": ("capex_to_revenue", "capex_to_revenue_pct"),
    "cash_conversion_cycle": ("cash_conversion_cycle",),
    "npl_ratio_pct": ("npl_ratio", "npl_ratio_pct", "non_performing_loan_ratio"),
    "provision_coverage_pct": ("provision_coverage", "provision_coverage_pct"),
    "cet1_ratio_pct": ("cet1_ratio", "cet1_ratio_pct", "core_tier1_ratio"),
    "net_interest_margin_pct": ("net_interest_margin", "net_interest_margin_pct", "nim"),
    "cost_income_ratio_pct": ("cost_income_ratio", "cost_income_ratio_pct"),
    "volatility_earnings_pct": ("volatility_earnings", "earnings_volatility", "earnings_volatility_pct"),
}


def normalize_fundamental_metrics(metrics: Optional[Dict[str, Any]]) -> Dict[str, float]:
    raw = metrics or {}
    lowered = {str(k).strip().lower(): v for k, v in raw.items()}
    out: Dict[str, float] = {}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            val = _safe_float(lowered.get(alias.lower()))
            if val is not None:
                out[canonical] = val
                break
    return out


def select_fundamental_model(
    *,
    symbol: str = "",
    name: str = "",
    sector: str = "",
    industry: str = "",
    market: str = "",
) -> str:
    sym = (symbol or "").strip().upper()
    if sym in KNOWN_SYMBOL_MODELS:
        return KNOWN_SYMBOL_MODELS[sym]

    text = " ".join([name or "", sector or "", industry or "", market or ""]).lower()
    for model_key, keywords in KEYWORD_MODELS:
        if any(keyword.lower() in text for keyword in keywords):
            return model_key
    return "generic_multifactor"


def assess_fundamentals(
    *,
    symbol: str,
    name: str = "",
    sector: str = "",
    industry: str = "",
    market: str = "",
    metrics: Optional[Dict[str, Any]] = None,
) -> FundamentalAssessment:
    model_key = select_fundamental_model(
        symbol=symbol, name=name, sector=sector, industry=industry, market=market,
    )
    factors = MODEL_FACTORS[model_key]
    normalized = normalize_fundamental_metrics(metrics)
    used: Dict[str, float] = {}
    scores: Dict[str, float] = {}
    weighted_sum = 0.0
    used_weight = 0.0
    total_weight = sum(f.weight for f in factors)

    for factor in factors:
        value = normalized.get(factor.name)
        if value is None:
            continue
        score = factor.score_fn(value)
        used[factor.name] = value
        scores[factor.name] = round(score, 4)
        weighted_sum += factor.weight * score
        used_weight += factor.weight

    coverage = used_weight / total_weight if total_weight > 0 else 0.0
    low_confidence = coverage < 0.25
    composite = 0.0 if used_weight <= 0 else weighted_sum / used_weight
    if low_confidence:
        composite *= coverage / 0.25

    composite = _clamp(composite, -1.0, 1.0)
    score = round(50.0 + composite * 50.0, 2)
    # Fundamentals are slow-moving. They should anchor sizing more than they
    # should dominate the 30d regime-conditioned trading signal.
    active_coverage = 0.0 if low_confidence else coverage
    anchor_multiplier = round(_clamp(1.0 + 0.30 * composite * active_coverage, 0.75, 1.25), 4)
    expected_return_adjustment_pct = round(_clamp(1.5 * composite * active_coverage, -1.5, 1.5), 4)
    reason = (
        "insufficient_fundamental_metric_coverage_neutral"
        if low_confidence
        else "sector_model_weighted_fundamental_score"
    )

    return FundamentalAssessment(
        symbol=(symbol or "").upper(),
        model_key=model_key,
        model_name=MODEL_NAMES[model_key],
        sector=sector,
        industry=industry,
        score=score,
        composite=round(composite, 4),
        coverage=round(coverage, 4),
        anchor_multiplier=anchor_multiplier,
        expected_return_adjustment_pct=expected_return_adjustment_pct,
        low_confidence=low_confidence,
        used_metrics=used,
        factor_scores=scores,
        reason=reason,
    )


__all__ = [
    "FundamentalAssessment",
    "assess_fundamentals",
    "normalize_fundamental_metrics",
    "select_fundamental_model",
]
