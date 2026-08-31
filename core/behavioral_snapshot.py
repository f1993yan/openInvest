"""Attach persisted behavioral-factor targets to monitor API snapshots.

This module is deliberately deterministic and side-effect free. The factor
calculation job owns market-data refreshes and state writes; API requests only
read that state and merge it into existing snapshot rows.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from zoneinfo import ZoneInfo


DEFAULT_BEHAVIORAL_STATE_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "behavioral_factor_state.json"
)
DEFAULT_BEHAVIORAL_ASSESSMENTS_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "behavioral_factor_assessments.json"
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
    try:
        file_updated_at = datetime.fromtimestamp(
            path.stat().st_mtime,
            tz=ZoneInfo("Asia/Shanghai"),
        )
        if file_updated_at.date().isoformat() == str(payload.get("rebalance_date") or "")[:10]:
            payload["_legacy_state_updated_at"] = file_updated_at.isoformat(
                timespec="seconds"
            )
    except OSError:
        pass
    return payload


def _load_assessments(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("assessments"), dict):
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


def _selected_target_weights(raw_targets: Mapping[str, Any]) -> Dict[str, float]:
    normalized = _normalize_targets(raw_targets)
    ranked = sorted(
        (
            (symbol, target)
            for symbol, target in normalized.items()
            if target.get("selected") is True
        ),
        key=lambda item: (
            -_safe_float(item[1].get("target_weight_pct")),
            item[0],
        ),
    )[:MAX_BEHAVIORAL_TARGETS]
    return {
        symbol: round(_safe_float(target.get("target_weight_pct")), 4)
        for symbol, target in ranked
    }


def _next_target_effective_at(rebalance_date: str, rebalance_sessions: Any) -> str:
    """Return the next session open after the frozen holding window completes."""
    try:
        anchor = date.fromisoformat(str(rebalance_date)[:10])
        session_count = max(1, int(rebalance_sessions))
        import exchange_calendars as xcal

        calendar = xcal.get_calendar("XSHG")
        completed_session = calendar.date_to_session(anchor.isoformat(), direction="next")
        if completed_session.date() <= anchor:
            completed_session = calendar.next_session(completed_session)
        for _ in range(session_count - 1):
            completed_session = calendar.next_session(completed_session)
        effective_session = calendar.next_session(completed_session)
        effective_at = datetime.combine(
            effective_session.date(),
            time(9, 30),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        return effective_at.isoformat(timespec="minutes")
    except (TypeError, ValueError, OverflowError, ImportError):
        return ""


def _parse_local_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed


def _timing_summary(
    state: Mapping[str, Any],
    assessments: Mapping[str, Any],
    *,
    selected_symbols: set[str],
) -> Dict[str, Any]:
    assessment_scope = str(assessments.get("selection_scope") or "")
    state_scope = str(state.get("selection_scope") or "factor_model_target_portfolio")
    assessed_targets = (
        _selected_target_weights(assessments.get("assessments") or {})
        if not assessment_scope or assessment_scope == state_scope
        else {}
    )
    current_targets = _selected_target_weights(state.get("targets") or {})
    current_targets = {
        symbol: weight
        for symbol, weight in current_targets.items()
        if symbol in selected_symbols
    }
    rebalance_date = str(state.get("rebalance_date") or "")
    assessment_updated_at = str(assessments.get("date") or "")
    legacy_state_updated_at = str(state.get("_legacy_state_updated_at") or "")
    state_score_updated_at = str(
        state.get("score_updated_at") or legacy_state_updated_at
    )
    state_reference_at = str(
        state_score_updated_at
        or state.get("targets_effective_at")
        or rebalance_date
    )
    assessment_timestamp = _parse_local_timestamp(assessment_updated_at)
    state_score_timestamp = _parse_local_timestamp(state_score_updated_at)
    state_timestamp = _parse_local_timestamp(state_reference_at)
    assessment_is_newer = bool(
        assessment_timestamp
        and (state_timestamp is None or assessment_timestamp > state_timestamp)
    )
    pending = bool(
        assessment_is_newer
        and assessed_targets
        and assessed_targets != current_targets
    )
    if pending:
        target_effective_at = _next_target_effective_at(
            rebalance_date,
            state.get("rebalance_sessions", 5),
        )
        effective_status = "scheduled"
    else:
        target_effective_at = str(
            state.get("targets_effective_at")
            or legacy_state_updated_at
            or rebalance_date
        )
        effective_status = "active"
    return {
        "score_updated_at": (
            state_score_updated_at
            if state_score_timestamp
            and (
                assessment_timestamp is None
                or state_score_timestamp >= assessment_timestamp
            )
            else assessment_updated_at or state_score_updated_at
        ),
        "target_effective_at": target_effective_at,
        "target_effective_status": effective_status,
        "pending_target_change": pending,
    }


def enrich_snapshot_with_behavioral_targets(
    payload: Dict[str, Any],
    *,
    state_path: Optional[Path] = None,
    assessments_path: Optional[Path] = None,
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

    assessment_file = assessments_path or DEFAULT_BEHAVIORAL_ASSESSMENTS_PATH
    assessments = _load_assessments(assessment_file)

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
        **_timing_summary(state, assessments, selected_symbols=selected_symbols),
    }
    return result


__all__ = [
    "DEFAULT_BEHAVIORAL_ASSESSMENTS_PATH",
    "DEFAULT_BEHAVIORAL_STATE_PATH",
    "MAX_BEHAVIORAL_TARGETS",
    "enrich_snapshot_with_behavioral_targets",
]
