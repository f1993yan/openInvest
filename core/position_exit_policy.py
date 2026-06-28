"""A-share position exit policy loaded from local .env.

This module is intentionally separate from ``core.entry_exit_points``.  The
policy here is for already-held A-share positions anchored to actual cost.  It
must not be used as a buy-entry or technical entry/exit point calculator.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class PositionExitPolicy:
    max_loss_pct: float
    stop_atr_mult: float
    take_profit_r1: float
    take_profit_r2: float
    trailing_atr_mult: float
    policy_quality_score: float = 0.0
    objective_score: float = 0.0
    sell_count: int = 0
    sell_win_count: int = 0
    sell_win_rate: float = 0.0
    sell_win_rate_lower: float = 0.0
    profit_factor: float = 0.0
    conservative_sell_expectancy_cny: float = 0.0
    sell_path_sample_count: int = 0
    avg_post_sell_avoided_drawdown_pct: float = 0.0
    avg_post_sell_missed_rebound_pct: float = 0.0
    avg_post_sell_net_edge_pct: float = 0.0
    post_sell_positive_edge_rate: float = 0.0
    post_sell_positive_edge_lower: float = 0.0
    sell_utility_adjustment_pct: float = 0.0
    sell_reliability: float = 0.0
    sell_evidence_score: float = 0.0
    max_drawdown_pct: float = 0.0
    source: str = ".env"
    sector: str = ""
    sample_symbols: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def audit_text(
        self,
        *,
        symbol: str = "",
        market: str = "a",
        is_holding: bool = False,
        cost: float = 0.0,
        current_price: float = 0.0,
    ) -> str:
        status = "active_for_existing_position" if market == "a" and is_holding else "context_only"
        pnl = 0.0
        if cost > 0 and current_price > 0:
            pnl = (current_price / cost - 1.0) * 100.0
        sector_text = self.sector or "global"
        sample_text = self.sample_symbols or "global"
        return (
            "\n\n[POSITION_EXIT_POLICY]\n"
            "scope=existing_a_share_position_only\n"
            f"symbol={symbol.upper()} market={market} sector={sector_text} "
            f"status={status} source={self.source} samples={sample_text}\n"
            "boundary=Do not use this block to compute buy_pullback_price, "
            "buy_breakout_price, reentry_price, or technical entry/exit points.\n"
            f"holding_cost={cost:.2f} current_price={current_price:.2f} unrealized_pnl_pct={pnl:+.2f}%\n"
            f"max_loss_pct={self.max_loss_pct:.4f} stop_atr_mult={self.stop_atr_mult:.4f} "
            f"take_profit_r1={self.take_profit_r1:.4f} take_profit_r2={self.take_profit_r2:.4f} "
            f"trailing_atr_mult={self.trailing_atr_mult:.4f}\n"
            f"sell_quality=policy_quality_score={self.policy_quality_score:.4f} "
            f"sell_win_rate_lower={self.sell_win_rate_lower:.4f} "
            f"conservative_expectancy_cny={self.conservative_sell_expectancy_cny:.2f} "
            f"post_sell_net_edge_pct={self.avg_post_sell_net_edge_pct:.4f} "
            f"post_sell_positive_edge_lower={self.post_sell_positive_edge_lower:.4f}\n"
            f"sell_utility_adjustment_pct={self.sell_utility_adjustment_pct:.4f} "
            f"sell_reliability={self.sell_reliability:.4f} "
            f"sell_evidence_score={self.sell_evidence_score:.4f}\n"
            "formula=initial_stop_pct=min(max_loss_pct,max(3,atr_pct*stop_atr_mult)); "
            "hard_stop=cost*(1-initial_stop_pct/100); "
            "take_profit_1=cost+R*take_profit_r1; take_profit_2=cost+R*take_profit_r2; "
            "trailing_stop only moves upward after A-share close.\n"
            "right_side_note=Right-side entries must be validated by ENTRY_EXIT_POINTS/optimizer "
            "conditional expectation; this policy only defines exits after a real position exists.\n"
            "review_task=When reviewing an existing holding, judge whether the action respects "
            "this sector-specific cost-anchored discipline; for new entries, only treat it as risk context."
        )


def _normalize_sector(sector: str) -> str:
    return str(sector or "").strip() or "未分组"


def _decode_sector_policies(raw: str) -> Dict[str, Dict[str, Any]]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for sector, value in parsed.items():
        if isinstance(value, dict):
            out[_normalize_sector(sector)] = value
    return out


def _policy_from_values(values: Dict[str, Any], *, source: str, sector: str = "") -> PositionExitPolicy:
    sell_count = int(max(0, _safe_float(values.get("sell_count"), 0.0)))
    sell_path_sample_count = int(max(0, _safe_float(values.get("sell_path_sample_count"), 0.0)))
    sell_reliability = _clamp(_safe_float(values.get("sell_reliability"), _reliability(sell_count, sell_path_sample_count)), 0.0, 1.0)
    sell_utility_adjustment_pct = _clamp(
        _safe_float(
            values.get("sell_utility_adjustment_pct"),
            _derived_sell_utility_adjustment_pct(values, reliability=sell_reliability),
        ),
        -8.0,
        8.0,
    )
    return PositionExitPolicy(
        max_loss_pct=_clamp(_safe_float(values.get("max_loss_pct"), 8.0), 1.0, 15.0),
        stop_atr_mult=max(0.5, _safe_float(values.get("stop_atr_mult"), 2.0)),
        take_profit_r1=max(0.5, _safe_float(values.get("take_profit_r1"), 1.5)),
        take_profit_r2=max(1.0, _safe_float(values.get("take_profit_r2"), 2.5)),
        trailing_atr_mult=max(0.5, _safe_float(values.get("trailing_atr_mult"), 2.8)),
        policy_quality_score=_safe_float(values.get("policy_quality_score"), 0.0),
        objective_score=_safe_float(values.get("objective_score"), 0.0),
        sell_count=sell_count,
        sell_win_count=int(max(0, _safe_float(values.get("sell_win_count"), 0.0))),
        sell_win_rate=_clamp(_safe_float(values.get("sell_win_rate"), 0.0), 0.0, 1.0),
        sell_win_rate_lower=_clamp(_safe_float(values.get("sell_win_rate_lower"), 0.0), 0.0, 1.0),
        profit_factor=max(0.0, _safe_float(values.get("profit_factor"), 0.0)),
        conservative_sell_expectancy_cny=_safe_float(values.get("conservative_sell_expectancy_cny"), 0.0),
        sell_path_sample_count=sell_path_sample_count,
        avg_post_sell_avoided_drawdown_pct=_safe_float(values.get("avg_post_sell_avoided_drawdown_pct"), 0.0),
        avg_post_sell_missed_rebound_pct=_safe_float(values.get("avg_post_sell_missed_rebound_pct"), 0.0),
        avg_post_sell_net_edge_pct=_safe_float(values.get("avg_post_sell_net_edge_pct"), 0.0),
        post_sell_positive_edge_rate=_clamp(_safe_float(values.get("post_sell_positive_edge_rate"), 0.0), 0.0, 1.0),
        post_sell_positive_edge_lower=_clamp(_safe_float(values.get("post_sell_positive_edge_lower"), 0.0), 0.0, 1.0),
        sell_utility_adjustment_pct=sell_utility_adjustment_pct,
        sell_reliability=sell_reliability,
        sell_evidence_score=_safe_float(values.get("sell_evidence_score"), sell_utility_adjustment_pct / 8.0),
        max_drawdown_pct=_safe_float(values.get("max_drawdown_pct"), 0.0),
        source=source,
        sector=sector,
        sample_symbols=",".join(str(x) for x in (values.get("sample_symbols") or []) if x),
    )


def _reliability(sell_count: int, sell_path_sample_count: int) -> float:
    """Shrink sparse weekly sell evidence toward zero.

    The denominator is a pseudo-count, not a trading threshold: with only one
    or two historical sells, the model should explain the signal but barely
    move executable utility.
    """
    n = max(0, min(sell_count, sell_path_sample_count) if sell_path_sample_count else sell_count)
    return n / (n + 8.0) if n > 0 else 0.0


def _derived_sell_utility_adjustment_pct(values: Dict[str, Any], *, reliability: float) -> float:
    """Convert weekly sell diagnostics into a conservative 30d utility haircut.

    The sign and scale come from path statistics:
    - positive edge means selling historically avoided more drawdown than it
      missed rebound, so holding should receive a negative utility adjustment;
    - Wilson lower bounds keep the effect near zero until repeated evidence
      exists;
    - negative edge can mildly discourage premature sells.
    """
    net_edge = _safe_float(values.get("avg_post_sell_net_edge_pct"), 0.0)
    avoided = _safe_float(values.get("avg_post_sell_avoided_drawdown_pct"), 0.0)
    missed = _safe_float(values.get("avg_post_sell_missed_rebound_pct"), 0.0)
    path_scale = max(abs(net_edge), avoided + missed, 1.0)
    path_utility = _clamp(net_edge / path_scale, -1.0, 1.0)
    win_lower = _safe_float(values.get("sell_win_rate_lower"), 0.0)
    path_lower = _safe_float(values.get("post_sell_positive_edge_lower"), 0.0)
    conservative_probability = _clamp(0.5 * win_lower + 0.5 * path_lower, 0.0, 1.0)
    evidence = _clamp((conservative_probability - 0.5) * 2.0, -1.0, 1.0)
    # 8 percentage points is the hard cap for a 30d expected utility haircut.
    # The realized value is usually much smaller because reliability and
    # Wilson-lower evidence shrink it toward zero.
    return 8.0 * reliability * (0.65 * path_utility + 0.35 * evidence)


def _safe_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _read_dotenv(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    values: Dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        return {}
    return values


def _env_value(key: str, default: str, values: Dict[str, str]) -> str:
    return values.get(key) or os.getenv(key, default)


def load_position_exit_policy(env_path: Optional[Path] = None, sector: str = "") -> PositionExitPolicy:
    explicit_env_path = env_path is not None
    env_path = env_path or (ROOT / ".env")
    values = _read_dotenv(env_path)
    normalized_sector = _normalize_sector(sector)

    sector_policies = {}
    json_paths = [] if explicit_env_path else [
        ROOT / "reports" / "weekly_exit_param_optimization.json",
        Path("/data/data/com.f1993yan.openInvest/files/weekly_exit_param_optimization.json"),
        ROOT / "weekly_exit_param_optimization.json",
    ]
    for path in json_paths:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                policies = data.get("sector_exit_policies") or data.get("sector_policies")
                if policies:
                    if isinstance(policies, dict):
                        sector_policies = {
                            _normalize_sector(k).lower(): v
                            for k, v in policies.items()
                            if isinstance(v, dict)
                        }
                    elif isinstance(policies, str):
                        sector_policies = {k.lower(): v for k, v in _decode_sector_policies(policies).items()}
                    if sector_policies:
                        break
            except Exception:
                pass

    if not sector_policies:
        sector_policies = {
            k.lower(): v
            for k, v in _decode_sector_policies(
                _env_value("INVEST_A_SHARE_SECTOR_EXIT_POLICIES", "", values)
            ).items()
        }

    if normalized_sector.lower() in sector_policies:
        return _policy_from_values(
            sector_policies[normalized_sector.lower()],
            source=f"sector_exit_policies#sector:{normalized_sector}",
            sector=normalized_sector,
        )

    fallback = {
        "max_loss_pct": _env_value("INVEST_A_SHARE_POSITION_MAX_LOSS_PCT", "8", values),
        "stop_atr_mult": _env_value("INVEST_A_SHARE_POSITION_STOP_ATR_MULT", "2.0", values),
        "take_profit_r1": _env_value("INVEST_A_SHARE_POSITION_TAKE_PROFIT_R1", "1.5", values),
        "take_profit_r2": _env_value("INVEST_A_SHARE_POSITION_TAKE_PROFIT_R2", "2.5", values),
        "trailing_atr_mult": _env_value("INVEST_A_SHARE_POSITION_TRAIL_ATR_MULT", "2.8", values),
    }
    return _policy_from_values(fallback, source=str(env_path), sector="global")


__all__ = ["PositionExitPolicy", "load_position_exit_policy"]
