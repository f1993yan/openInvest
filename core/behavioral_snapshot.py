"""Attach persisted behavioral-factor targets to monitor API snapshots.

This module is deliberately deterministic and side-effect free. The factor
calculation job owns market-data refreshes and state writes; API requests only
read that state and merge it into existing snapshot rows.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


DEFAULT_BEHAVIORAL_STATE_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "behavioral_factor_state.json"
)
MAX_BEHAVIORAL_TARGETS = 4


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _canonical_a_share_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    digits = "".join(char for char in text if char.isdigit())
    return digits if len(digits) == 6 else ""


def _load_state(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("targets"), dict):
        return {}
    return payload


def _normalize_targets(raw_targets: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    targets: Dict[str, Dict[str, Any]] = {}
    for raw_symbol, raw_target in raw_targets.items():
        symbol = _canonical_a_share_symbol(raw_symbol)
        if not symbol or not isinstance(raw_target, dict):
            continue
        targets[symbol] = dict(raw_target)
    return targets


def enrich_snapshot_with_behavioral_targets(
    payload: Dict[str, Any],
    *,
    state_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Merge the persisted target portfolio into a monitor snapshot.

    Unknown top-level fields and row fields are preserved for API backward
    compatibility. A corrupted state cannot expose more than four selected
    rows, and an unavailable state leaves the original response untouched.
    """
    path = state_path or DEFAULT_BEHAVIORAL_STATE_PATH
    state = _load_state(path)
    targets = _normalize_targets(state.get("targets") or {})
    if not targets:
        return payload

    ranked_selected = sorted(
        (
            (symbol, target)
            for symbol, target in targets.items()
            if target.get("selected") is True
        ),
        key=lambda item: (
            -_safe_float(item[1].get("target_weight_pct")),
            item[0],
        ),
    )[:MAX_BEHAVIORAL_TARGETS]
    selected_symbols = {symbol for symbol, _target in ranked_selected}
    model_key = str(state.get("model") or "a_share_behavioral_v1")
    selection_scope = str(
        state.get("selection_scope") or "factor_model_target_portfolio"
    )
    rebalance_date = str(state.get("rebalance_date") or "")

    rows = []
    matched_selected = 0
    for raw_row in payload.get("rows") or []:
        if not isinstance(raw_row, dict):
            rows.append(raw_row)
            continue
        row = dict(raw_row)
        symbol = _canonical_a_share_symbol(row.get("symbol"))
        current_factor = row.get("behavioral_factor")
        factor = dict(current_factor) if isinstance(current_factor, dict) else {}
        target = targets.get(symbol)

        if target is not None:
            selected = symbol in selected_symbols
            factor.update(
                {
                    "model_key": model_key,
                    "selected": selected,
                    "target_weight_pct": (
                        _safe_float(target.get("target_weight_pct")) if selected else 0.0
                    ),
                    "selection_scope": selection_scope,
                    "represents_account_holding": False,
                    "rebalance_date": rebalance_date,
                    "source": "behavioral_factor_state",
                }
            )
            if selected:
                factor["low_confidence"] = False
                matched_selected += 1
            row["behavioral_factor"] = factor
        elif factor.get("selection_scope") == selection_scope:
            # The persisted state is authoritative for target membership.
            factor["selected"] = False
            factor["target_weight_pct"] = 0.0
            row["behavioral_factor"] = factor
        rows.append(row)

    result = dict(payload)
    result["rows"] = rows
    result["behavioral_factor_summary"] = {
        "model_key": model_key,
        "selection_scope": selection_scope,
        "rebalance_date": rebalance_date,
        "candidate_count": len(targets),
        "selected_count": len(selected_symbols),
        "matched_selected_count": matched_selected,
        "source": "behavioral_factor_state",
    }
    return result


__all__ = [
    "DEFAULT_BEHAVIORAL_STATE_PATH",
    "MAX_BEHAVIORAL_TARGETS",
    "enrich_snapshot_with_behavioral_targets",
]
