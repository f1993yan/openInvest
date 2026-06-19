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
    return PositionExitPolicy(
        max_loss_pct=_clamp(_safe_float(values.get("max_loss_pct"), 8.0), 1.0, 15.0),
        stop_atr_mult=max(0.5, _safe_float(values.get("stop_atr_mult"), 2.0)),
        take_profit_r1=max(0.5, _safe_float(values.get("take_profit_r1"), 1.5)),
        take_profit_r2=max(1.0, _safe_float(values.get("take_profit_r2"), 2.5)),
        trailing_atr_mult=max(0.5, _safe_float(values.get("trailing_atr_mult"), 2.8)),
        source=source,
        sector=sector,
        sample_symbols=",".join(str(x) for x in (values.get("sample_symbols") or []) if x),
    )


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
    # Prefer .env on each call so weekly optimization takes effect without restart.
    return values.get(key) or os.getenv(key, default)


def load_position_exit_policy(env_path: Optional[Path] = None, sector: str = "") -> PositionExitPolicy:
    env_path = env_path or (ROOT / ".env")
    values = _read_dotenv(env_path)
    normalized_sector = _normalize_sector(sector)
    sector_policies = _decode_sector_policies(_env_value("INVEST_A_SHARE_SECTOR_EXIT_POLICIES", "", values))
    if normalized_sector in sector_policies:
        return _policy_from_values(
            sector_policies[normalized_sector],
            source=f"{env_path}#sector:{normalized_sector}",
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
