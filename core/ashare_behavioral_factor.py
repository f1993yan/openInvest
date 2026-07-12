"""Production A-share behavioral factor.

The model is the production counterpart of the walk-forward research factor:
medium/long momentum dominates, falling turnover and low realized volatility
are secondary preferences, and stocks must have positive 60-day momentum and
trade above EMA100.  All calculations are point-in-time and use only bars at
or before ``as_of``.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence

import pandas as pd


@dataclass(frozen=True)
class AShareBehavioralAssessment:
    symbol: str
    score: float = 0.0
    expected_return_pct: float = 0.0
    target_weight_pct: float = 0.0
    eligible: bool = False
    selected: bool = False
    low_confidence: bool = True
    sample_size: int = 0
    model_key: str = "a_share_behavioral_v1"
    components: Dict[str, float] = field(default_factory=dict)
    reason: str = "insufficient_history"
    optimizer_weight: float = 0.55
    trailing_3m_factor_return_pct: float = 0.0
    trailing_3m_hit_rate: float = 0.5
    trailing_3m_sample_size: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]]) -> Optional["AShareBehavioralAssessment"]:
        if not value:
            return None
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value[key] for key in allowed if key in value})

    def audit_text(self) -> str:
        return (
            "\n\n[ASHARE_BEHAVIORAL_FACTOR]\n"
            f"model={self.model_key} score={self.score:.2f} eligible={str(self.eligible).lower()} "
            f"selected={str(self.selected).lower()} target_weight={self.target_weight_pct:.2f}%\n"
            f"expected_return_20d={self.expected_return_pct:+.2f}% calibration_n={self.sample_size} "
            f"low_confidence={str(self.low_confidence).lower()}\n"
            f"optimizer_weight={self.optimizer_weight:.3f} "
            f"trailing_3m_factor_return={self.trailing_3m_factor_return_pct:+.2f}% "
            f"trailing_3m_hit_rate={self.trailing_3m_hit_rate:.2%} "
            f"trailing_3m_n={self.trailing_3m_sample_size}\n"
            f"reason={self.reason}"
        )


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
    renamed: Dict[Any, str] = {}
    for canonical in ("Close", "Volume"):
        source = aliases.get(canonical.lower())
        if source is not None:
            renamed[source] = canonical
    data = data.rename(columns=renamed)
    if "Close" not in data:
        return pd.DataFrame()
    data["Close"] = pd.to_numeric(data["Close"], errors="coerce")
    if "Volume" not in data:
        data["Volume"] = float("nan")
    data["Volume"] = pd.to_numeric(data["Volume"], errors="coerce")
    return data.dropna(subset=["Close"])


def _period_return(close: pd.Series, periods: int) -> float:
    if len(close) <= periods:
        return float("nan")
    start = _safe_float(close.iloc[-periods - 1])
    end = _safe_float(close.iloc[-1])
    return end / start - 1.0 if start > 0 and end > 0 else float("nan")


def _feature_row(frame: pd.DataFrame) -> Optional[Dict[str, float]]:
    if len(frame) < 253:
        return None
    close = frame["Close"]
    returns = close.pct_change().dropna()
    volume = frame["Volume"]
    vol20 = _safe_float(returns.tail(20).std(ddof=1) * math.sqrt(252.0))
    volume20 = _safe_float(volume.tail(20).mean())
    volume120 = _safe_float(volume.tail(120).mean())
    ema100 = _safe_float(close.ewm(span=100, adjust=False).mean().iloc[-1])
    return {
        "ret20": _period_return(close, 20),
        "ret60": _period_return(close, 60),
        "ret120": _period_return(close, 120),
        "ret252": _period_return(close, 252),
        "vol20": vol20,
        "volume_ratio20_120": volume20 / volume120 if volume20 > 0 and volume120 > 0 else float("nan"),
        "above_ema100": _safe_float(close.iloc[-1]) / ema100 - 1.0 if ema100 > 0 else float("nan"),
    }


def build_behavioral_feature_table(
    histories: Mapping[str, pd.DataFrame],
    *,
    as_of: Any = None,
) -> pd.DataFrame:
    rows: Dict[str, Dict[str, float]] = {}
    for raw_symbol, raw_frame in histories.items():
        symbol = str(raw_symbol).strip().upper()
        frame = _normalize_history(raw_frame, as_of)
        row = _feature_row(frame)
        if row is not None:
            rows[symbol] = row
    return pd.DataFrame.from_dict(rows, orient="index").replace([float("inf"), float("-inf")], float("nan"))


def _rank_high(values: pd.Series) -> pd.Series:
    return values.rank(method="average", pct=True, ascending=True)


def score_behavioral_features(features: pd.DataFrame) -> pd.DataFrame:
    if features.empty:
        return features.copy()
    scored = features.copy()
    momentum = (
        0.35 * _rank_high(scored["ret60"])
        + 0.30 * _rank_high(scored["ret120"])
        + 0.20 * _rank_high(scored["ret252"])
        + 0.15 * _rank_high(scored["ret20"])
    )
    low_turnover = _rank_high(-scored["volume_ratio20_120"])
    low_volatility = _rank_high(-scored["vol20"])
    scored["momentum_score"] = momentum
    scored["low_turnover_score"] = low_turnover
    scored["low_volatility_score"] = low_volatility
    scored["behavioral_score"] = 0.65 * momentum + 0.25 * low_turnover + 0.10 * low_volatility
    scored["eligible"] = (scored["ret60"] > 0) & (scored["above_ema100"] > 0)
    scored.loc[~scored["eligible"], "behavioral_score"] = float("nan")
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
    raw = {symbol: 1.0 / max(_safe_float(features.at[symbol, "vol20"], 0.30), 0.08) for symbol in selected}
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
    normalized = {str(symbol).upper(): _normalize_history(frame) for symbol, frame in histories.items()}
    dates = sorted({date for frame in normalized.values() for date in frame.index})
    records = []
    # Monthly-like walk-forward observations keep adjacent samples from being
    # counted as independent while matching the production rebalance horizon.
    for decision_date in dates[252:-20:20]:
        features = score_behavioral_features(build_behavioral_feature_table(normalized, as_of=decision_date))
        for symbol, row in features.iterrows():
            score = _safe_float(row.get("behavioral_score"))
            frame = normalized.get(symbol, pd.DataFrame())
            future = frame[frame.index > decision_date]["Close"].head(20)
            past = frame[frame.index <= decision_date]["Close"]
            if not math.isfinite(score) or len(future) < 20 or past.empty:
                continue
            start = _safe_float(past.iloc[-1])
            end = _safe_float(future.iloc[-1])
            if start > 0 and end > 0:
                records.append({"score": score, "forward_return_pct": (end / start - 1.0) * 100.0})
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
    # Empirical-Bayes shrinkage prevents a small nearest-neighbour sample from
    # producing extreme optimizer inputs.  Twenty prior observations equal one
    # rebalance year's evidence.
    shrunk = (n * local_mean + 20.0 * prior_mean) / (n + 20.0)
    return max(-15.0, min(15.0, shrunk)), n


def _trailing_factor_performance(
    histories: Mapping[str, pd.DataFrame],
    *,
    top_k: int,
) -> Dict[str, Dict[str, float]]:
    """Estimate per-symbol factor skill over the latest 63 sessions.

    Membership is frozen in five-session blocks.  A selected stock earns its
    next-day excess return over the local cross-section; an unselected stock
    earns the avoided excess return.  This measures whether the factor ranked
    that specific stock correctly without rewarding a broad market rally.
    """
    symbols = sorted(histories)
    if not symbols:
        return {}
    longest = max(histories.values(), key=len)
    calendar = list(longest.index[-64:])
    payoffs: Dict[str, list[float]] = {symbol: [] for symbol in symbols}
    selected: set[str] = set()
    for offset, decision_date in enumerate(calendar[:-1]):
        if offset % 5 == 0:
            features = score_behavioral_features(
                build_behavioral_feature_table(histories, as_of=decision_date)
            )
            ranked = features["behavioral_score"].dropna().sort_values(ascending=False)
            selected = set(ranked.head(max(1, int(top_k))).index)

        next_returns: Dict[str, float] = {}
        for symbol, frame in histories.items():
            past = frame[frame.index <= decision_date]["Close"]
            future = frame[frame.index > decision_date]["Close"].head(1)
            if past.empty or future.empty:
                continue
            start = _safe_float(past.iloc[-1])
            end = _safe_float(future.iloc[0])
            if start > 0 and end > 0:
                next_returns[symbol] = end / start - 1.0
        if len(next_returns) < 2:
            continue
        benchmark = sum(next_returns.values()) / len(next_returns)
        for symbol, stock_return in next_returns.items():
            excess = stock_return - benchmark
            payoffs[symbol].append(excess if symbol in selected else -excess)

    output: Dict[str, Dict[str, float]] = {}
    for symbol, values in payoffs.items():
        n = len(values)
        if not values:
            output[symbol] = {
                "optimizer_weight": 0.55,
                "return_pct": 0.0,
                "hit_rate": 0.5,
                "sample_size": 0,
                "posterior_positive_probability": 0.5,
            }
            continue
        series = pd.Series(values, dtype=float)
        mean = _safe_float(series.mean(), 0.0)
        volatility = max(_safe_float(series.std(ddof=1), 0.0), 0.005)
        reliability = n / (n + 20.0)
        posterior_mean = reliability * mean
        posterior_se = volatility / math.sqrt(n + 20.0)
        z_score = posterior_mean / max(posterior_se, 1e-9)
        probability = 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))
        optimizer_weight = 0.55 + 0.45 * reliability * probability
        cumulative = float((1.0 + series).prod() - 1.0)
        output[symbol] = {
            "optimizer_weight": round(max(0.55, min(1.0, optimizer_weight)), 6),
            "return_pct": round(cumulative * 100.0, 4),
            "hit_rate": round(float((series > 0).mean()), 6),
            "sample_size": n,
            "posterior_positive_probability": round(probability, 6),
        }
    return output


def assess_behavioral_universe(
    histories: Mapping[str, pd.DataFrame],
    *,
    as_of: Any = None,
    top_k: int = 4,
    gross_exposure: float = 0.95,
    max_weight: float = 0.35,
) -> Dict[str, AShareBehavioralAssessment]:
    point_in_time_histories = {
        str(symbol).strip().upper(): _normalize_history(frame, as_of)
        for symbol, frame in histories.items()
    }
    features = score_behavioral_features(build_behavioral_feature_table(point_in_time_histories))
    all_symbols = {str(symbol).strip().upper() for symbol in histories}
    if features.empty:
        return {symbol: AShareBehavioralAssessment(symbol=symbol) for symbol in all_symbols}
    ranked = features["behavioral_score"].dropna().sort_values(ascending=False)
    selected = list(ranked.head(max(1, int(top_k))).index)
    weights = _inverse_volatility_weights(selected, features, gross_exposure=gross_exposure, max_weight=max_weight)
    samples = _calibration_samples(point_in_time_histories)
    trailing_performance = _trailing_factor_performance(
        point_in_time_histories,
        top_k=top_k,
    )
    assessments: Dict[str, AShareBehavioralAssessment] = {}
    cross_section_ok = len(features) >= 4
    for symbol in all_symbols:
        if symbol not in features.index:
            assessments[symbol] = AShareBehavioralAssessment(symbol=symbol)
            continue
        row = features.loc[symbol]
        score = _safe_float(row.get("behavioral_score"), 0.0)
        eligible = bool(row.get("eligible", False)) and math.isfinite(_safe_float(row.get("behavioral_score")))
        expected_return, sample_size = _calibrated_return(score, samples)
        is_selected = symbol in selected
        performance = trailing_performance.get(symbol) or {}
        reason = (
            "top4_positive_momentum_low_turnover_low_volatility"
            if is_selected else
            "eligibility_gate_failed" if not eligible else "outside_top4_cross_section"
        )
        assessments[symbol] = AShareBehavioralAssessment(
            symbol=symbol,
            score=round(score * 100.0, 4) if eligible else 0.0,
            expected_return_pct=round(expected_return, 4),
            target_weight_pct=round(weights.get(symbol, 0.0) * 100.0, 4),
            eligible=eligible,
            selected=is_selected,
            low_confidence=not cross_section_ok or sample_size < 20,
            sample_size=sample_size,
            components={
                "momentum": round(_safe_float(row.get("momentum_score"), 0.0) * 100.0, 4),
                "low_turnover": round(_safe_float(row.get("low_turnover_score"), 0.0) * 100.0, 4),
                "low_volatility": round(_safe_float(row.get("low_volatility_score"), 0.0) * 100.0, 4),
                "ret60_pct": round(_safe_float(row.get("ret60"), 0.0) * 100.0, 4),
                "above_ema100_pct": round(_safe_float(row.get("above_ema100"), 0.0) * 100.0, 4),
                "trailing_3m_p_positive": round(
                    _safe_float(performance.get("posterior_positive_probability"), 0.5), 6,
                ),
            },
            reason=reason,
            optimizer_weight=_safe_float(performance.get("optimizer_weight"), 0.55),
            trailing_3m_factor_return_pct=_safe_float(performance.get("return_pct"), 0.0),
            trailing_3m_hit_rate=_safe_float(performance.get("hit_rate"), 0.5),
            trailing_3m_sample_size=int(performance.get("sample_size", 0) or 0),
        )
    return assessments


def assess_single_behavioral_history(
    symbol: str,
    history: pd.DataFrame,
    *,
    as_of: Any = None,
) -> AShareBehavioralAssessment:
    """Compatibility fallback for one-symbol API calls.

    A one-stock universe cannot provide meaningful cross-sectional ranks, so
    it remains explicitly low confidence and never claims top-four selection.
    """
    assessments = assess_behavioral_universe({symbol: history}, as_of=as_of, top_k=1)
    value = assessments.get(str(symbol).upper(), AShareBehavioralAssessment(symbol=str(symbol).upper()))
    return AShareBehavioralAssessment(
        **{**value.as_dict(), "selected": False, "target_weight_pct": 0.0, "low_confidence": True,
           "reason": "single_symbol_fallback_" + value.reason}
    )
