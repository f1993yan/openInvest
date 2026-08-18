"""Production Hong Kong spatio-temporal momentum proxy.

This is the transparent linear proxy validated by the Hong Kong model
comparison, not a reproduction of the neural network in arXiv:2302.10175.
All features are point-in-time and use bars at or before ``as_of``.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence

import pandas as pd


MODEL_KEY = "hk_spatio_temporal_momentum_proxy_v1"
PAPER_URL = "https://arxiv.org/abs/2302.10175"
REFERENCE_UNIVERSE_KEY = "hsi_constituents_2026_08_18"
DEFAULT_HK_REFERENCE_UNIVERSE = (
    "00001", "00002", "00003", "00005", "00006", "00012", "00016", "00027",
    "00066", "00101", "00175", "00241", "00267", "00285", "00288", "00291",
    "00300", "00316", "00322", "00386", "00388", "00669", "00688", "00700",
    "00728", "00762", "00823", "00836", "00857", "00868", "00883", "00939",
    "00941", "00960", "00968", "00981", "00992", "01024", "01038", "01044",
    "01088", "01093", "01099", "01109", "01113", "01177", "01209", "01211",
    "01299", "01378", "01398", "01519", "01801", "01810", "01876", "01928",
    "01929", "01997", "02015", "02020", "02057", "02269", "02313", "02318",
    "02319", "02331", "02359", "02382", "02388", "02600", "02618", "02628",
    "02688", "02899", "03690", "03692", "03750", "03968", "03988", "03993",
    "06160", "06181", "06618", "06690", "06862", "09618", "09633", "09888",
    "09901", "09961", "09988", "09992", "09999",
)


@dataclass(frozen=True)
class HKSpatioTemporalAssessment:
    symbol: str
    score: float = 0.0
    expected_return_pct: float = 0.0
    target_weight_pct: float = 0.0
    eligible: bool = False
    selected: bool = False
    low_confidence: bool = True
    sample_size: int = 0
    model_key: str = MODEL_KEY
    components: Dict[str, float] = field(default_factory=dict)
    reason: str = "insufficient_history"
    optimizer_weight: float = 0.55
    rebalance_sessions: int = 20
    universe_size: int = 0
    reference_universe: str = REFERENCE_UNIVERSE_KEY
    selection_scope: str = "hk_factor_model_target_portfolio"
    represents_account_holding: bool = False
    paper_url: str = PAPER_URL

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(
        cls,
        value: Optional[Mapping[str, Any]],
    ) -> Optional["HKSpatioTemporalAssessment"]:
        if not value:
            return None
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value[key] for key in allowed if key in value})

    def audit_text(self) -> str:
        return (
            "\n\n[HK_SPATIO_TEMPORAL_FACTOR]\n"
            f"model={self.model_key} score={self.score:.2f} "
            f"eligible={str(self.eligible).lower()} selected={str(self.selected).lower()} "
            f"target_weight={self.target_weight_pct:.2f}%\n"
            f"expected_return_20d={self.expected_return_pct:+.2f}% "
            f"calibration_n={self.sample_size} low_confidence={str(self.low_confidence).lower()}\n"
            f"optimizer_weight={self.optimizer_weight:.3f} "
            f"rebalance_sessions={self.rebalance_sessions} universe_size={self.universe_size} "
            f"reference_universe={self.reference_universe} paper={self.paper_url}\n"
            f"selection_scope={self.selection_scope} "
            f"represents_account_holding={str(self.represents_account_holding).lower()}\n"
            f"reason={self.reason}"
        )


def normalize_hk_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.endswith(".HK"):
        text = text[:-3]
    if text.startswith("HK"):
        text = text[2:]
    return text.zfill(5) if text.isdigit() and 1 <= len(text) <= 5 else ""


def _safe_float(value: Any, default: float = float("nan")) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _normalize_history(frame: Optional[pd.DataFrame], as_of: Any = None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    data.index = pd.to_datetime(data.index, errors="coerce")
    data = data[~data.index.isna()].sort_index()
    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        if cutoff.tzinfo is not None:
            cutoff = cutoff.tz_localize(None)
        data = data[data.index <= cutoff]
    aliases = {str(column).lower(): column for column in data.columns}
    source = aliases.get("close")
    if source is None:
        return pd.DataFrame()
    data = data.rename(columns={source: "Close"})
    data["Close"] = pd.to_numeric(data["Close"], errors="coerce")
    return data.dropna(subset=["Close"])


def _period_return(close: pd.Series, periods: int) -> float:
    if len(close) <= periods:
        return float("nan")
    start = _safe_float(close.iloc[-periods - 1])
    end = _safe_float(close.iloc[-1])
    return end / start - 1.0 if start > 0 and end > 0 else float("nan")


def _feature_row(frame: pd.DataFrame) -> Optional[Dict[str, float]]:
    if len(frame) < 121:
        return None
    close = frame["Close"]
    returns = close.pct_change(fill_method=None).dropna()
    vol20 = _safe_float(returns.tail(20).std(ddof=1) * math.sqrt(252.0))
    vol60 = _safe_float(returns.tail(60).std(ddof=1) * math.sqrt(252.0))
    return {
        "ret20": _period_return(close, 20),
        "ret60": _period_return(close, 60),
        "ret120": _period_return(close, 120),
        "vol20": vol20,
        "vol60": vol60,
    }


def build_hk_spatio_feature_table(
    histories: Mapping[str, pd.DataFrame],
    *,
    as_of: Any = None,
) -> pd.DataFrame:
    rows: Dict[str, Dict[str, float]] = {}
    for raw_symbol, raw_frame in histories.items():
        symbol = normalize_hk_symbol(raw_symbol)
        if not symbol:
            continue
        row = _feature_row(_normalize_history(raw_frame, as_of))
        if row is not None:
            rows[symbol] = row
    return pd.DataFrame.from_dict(rows, orient="index").replace(
        [float("inf"), float("-inf")], float("nan"),
    )


def score_hk_spatio_features(features: pd.DataFrame) -> pd.DataFrame:
    """Apply the exact linear score used by the frozen backtest."""
    if features.empty:
        return features.copy()
    scored = features.copy()
    temporal = (
        scored["ret20"] / scored["vol20"].clip(lower=0.08)
        + scored["ret60"] / scored["vol60"].clip(lower=0.08)
        + scored["ret120"] / scored["vol60"].clip(lower=0.08)
    ) / 3.0
    scored["temporal_raw"] = temporal
    scored["temporal_rank"] = temporal.rank(
        method="average", pct=True, ascending=True,
    )
    scored["ret60_rank"] = scored["ret60"].rank(
        method="average", pct=True, ascending=True,
    )
    scored["eligible"] = (scored["ret20"] > 0.0) & (scored["ret60"] > 0.0)
    score = 0.55 * scored["temporal_rank"] + 0.45 * scored["ret60_rank"]
    scored["spatio_score"] = score.where(scored["eligible"], 0.0).fillna(0.0)
    return scored


def _inverse_volatility_weights(
    selected: Sequence[str],
    features: pd.DataFrame,
    *,
    gross_exposure: float,
    max_weight: float,
) -> Dict[str, float]:
    if not selected:
        return {}
    raw = {
        symbol: 1.0 / max(_safe_float(features.at[symbol, "vol20"], 0.30), 0.08)
        for symbol in selected
    }
    weights: Dict[str, float] = {}
    free = set(raw)
    remaining = gross_exposure
    while free and remaining > 1e-12:
        denominator = sum(raw[symbol] for symbol in free)
        proposed = {symbol: remaining * raw[symbol] / denominator for symbol in free}
        capped = {symbol for symbol, weight in proposed.items() if weight > max_weight}
        if not capped:
            weights.update(proposed)
            break
        for symbol in capped:
            weights[symbol] = max_weight
            free.remove(symbol)
            remaining -= max_weight
    return weights


def _calibration_samples(histories: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    normalized = {
        normalize_hk_symbol(symbol): _normalize_history(frame)
        for symbol, frame in histories.items()
        if normalize_hk_symbol(symbol)
    }
    dates = sorted({timestamp for frame in normalized.values() for timestamp in frame.index})
    records = []
    for decision_date in dates[120:-20:20]:
        features = score_hk_spatio_features(
            build_hk_spatio_feature_table(normalized, as_of=decision_date),
        )
        for symbol, row in features.iterrows():
            score = _safe_float(row.get("spatio_score"))
            frame = normalized.get(symbol, pd.DataFrame())
            past = frame[frame.index <= decision_date]["Close"]
            future = frame[frame.index > decision_date]["Close"].head(20)
            if not math.isfinite(score) or past.empty or len(future) < 20:
                continue
            start = _safe_float(past.iloc[-1])
            end = _safe_float(future.iloc[-1])
            if start > 0 and end > 0:
                records.append({
                    "score": score,
                    "forward_return_pct": (end / start - 1.0) * 100.0,
                })
    return pd.DataFrame.from_records(records)


def _calibrated_return(score: float, samples: pd.DataFrame) -> tuple[float, int]:
    if samples.empty or not math.isfinite(score):
        return 0.0, 0
    nearest = samples.assign(distance=(samples["score"] - score).abs()).nsmallest(40, "distance")
    if nearest.empty:
        return 0.0, 0
    local_mean = _safe_float(nearest["forward_return_pct"].mean(), 0.0)
    prior_mean = _safe_float(samples["forward_return_pct"].mean(), 0.0)
    n = len(nearest)
    # Twenty prior observations represent one full rebalance year.
    shrunk = (n * local_mean + 20.0 * prior_mean) / (n + 20.0)
    return max(-15.0, min(15.0, shrunk)), n


def assess_hk_spatio_universe(
    histories: Mapping[str, pd.DataFrame],
    *,
    as_of: Any = None,
    top_k: int = 4,
    gross_exposure: float = 0.95,
    max_weight: float = 0.35,
) -> Dict[str, HKSpatioTemporalAssessment]:
    point_in_time = {
        normalize_hk_symbol(symbol): _normalize_history(frame, as_of)
        for symbol, frame in histories.items()
        if normalize_hk_symbol(symbol)
    }
    all_symbols = set(point_in_time)
    features = score_hk_spatio_features(build_hk_spatio_feature_table(point_in_time))
    if features.empty:
        return {symbol: HKSpatioTemporalAssessment(symbol=symbol) for symbol in all_symbols}

    ranked = features["spatio_score"].replace(0.0, float("nan")).dropna().sort_values(ascending=False)
    selected = list(ranked.head(max(1, int(top_k))).index)
    weights = _inverse_volatility_weights(
        selected,
        features,
        gross_exposure=gross_exposure,
        max_weight=max_weight,
    )
    samples = _calibration_samples(point_in_time)
    cross_section_ok = len(features) >= 4
    assessments: Dict[str, HKSpatioTemporalAssessment] = {}
    for symbol in all_symbols:
        if symbol not in features.index:
            assessments[symbol] = HKSpatioTemporalAssessment(symbol=symbol)
            continue
        row = features.loc[symbol]
        score = _safe_float(row.get("spatio_score"), 0.0)
        eligible = bool(row.get("eligible", False)) and score > 0.0
        expected_return, sample_size = _calibrated_return(score, samples)
        is_selected = symbol in selected
        reliability = sample_size / (sample_size + 20.0) if sample_size > 0 else 0.0
        reason = (
            "top4_positive_20d_60d_spatio_temporal_momentum"
            if is_selected else
            "positive_momentum_gate_failed" if not eligible else "outside_top4_cross_section"
        )
        assessments[symbol] = HKSpatioTemporalAssessment(
            symbol=symbol,
            score=round(score * 100.0, 4),
            expected_return_pct=round(expected_return, 4),
            target_weight_pct=round(weights.get(symbol, 0.0) * 100.0, 4),
            eligible=eligible,
            selected=is_selected,
            low_confidence=not cross_section_ok or sample_size < 20,
            sample_size=sample_size,
            components={
                "temporal_rank": round(_safe_float(row.get("temporal_rank"), 0.0) * 100.0, 4),
                "ret60_rank": round(_safe_float(row.get("ret60_rank"), 0.0) * 100.0, 4),
                "ret20_pct": round(_safe_float(row.get("ret20"), 0.0) * 100.0, 4),
                "ret60_pct": round(_safe_float(row.get("ret60"), 0.0) * 100.0, 4),
                "ret120_pct": round(_safe_float(row.get("ret120"), 0.0) * 100.0, 4),
                "vol20_annualized_pct": round(_safe_float(row.get("vol20"), 0.0) * 100.0, 4),
            },
            reason=reason,
            optimizer_weight=round(0.55 + 0.45 * reliability, 6),
            universe_size=len(features),
        )
    return assessments


def assess_single_hk_spatio_history(
    symbol: str,
    history: pd.DataFrame,
    *,
    as_of: Any = None,
) -> HKSpatioTemporalAssessment:
    normalized = normalize_hk_symbol(symbol) or str(symbol).upper()
    assessment = assess_hk_spatio_universe(
        {normalized: history}, as_of=as_of, top_k=1,
    ).get(normalized, HKSpatioTemporalAssessment(symbol=normalized))
    return HKSpatioTemporalAssessment(
        **{
            **assessment.as_dict(),
            "selected": False,
            "target_weight_pct": 0.0,
            "low_confidence": True,
            "reason": "single_symbol_fallback_" + assessment.reason,
        }
    )


__all__ = [
    "HKSpatioTemporalAssessment",
    "DEFAULT_HK_REFERENCE_UNIVERSE",
    "MODEL_KEY",
    "PAPER_URL",
    "REFERENCE_UNIVERSE_KEY",
    "assess_hk_spatio_universe",
    "assess_single_hk_spatio_history",
    "build_hk_spatio_feature_table",
    "normalize_hk_symbol",
    "score_hk_spatio_features",
]
