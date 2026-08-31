from __future__ import annotations

import json
import os
from datetime import datetime

from fastapi.testclient import TestClient

from backend.server import app
from core.behavioral_snapshot import enrich_snapshot_with_behavioral_targets


def test_snapshot_uses_persisted_targets_and_caps_selection_at_four(tmp_path):
    state_path = tmp_path / "behavioral_factor_state.json"
    assessments_path = tmp_path / "behavioral_factor_assessments.json"
    state_path.write_text(
        json.dumps(
            {
                "model": "a_share_behavioral_v1",
                "selection_scope": "factor_model_target_portfolio",
                "rebalance_date": "2026-08-05",
                "rebalance_sessions": 5,
                "targets_effective_at": "2026-08-05T10:15:00+08:00",
                "targets": {
                    f"60000{index}": {
                        "selected": True,
                        "target_weight_pct": 10.0 + index,
                    }
                    for index in range(6)
                },
            }
        ),
        encoding="utf-8",
    )
    assessments_path.write_text(
        json.dumps(
            {
                "date": "2026-08-05T09:00:00+08:00",
                "selection_scope": "factor_model_target_portfolio",
                "assessments": {
                    f"60000{index}": {
                        "selected": True,
                        "target_weight_pct": 10.0 + index,
                    }
                    for index in range(2, 6)
                },
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "version": 1,
        "rows": [
            {
                "symbol": f"60000{index}",
                "behavioral_factor": {
                    "selected": index == 0,
                    "selection_scope": "factor_model_target_portfolio",
                },
            }
            for index in range(6)
        ],
    }

    enriched = enrich_snapshot_with_behavioral_targets(
        payload,
        state_path=state_path,
        assessments_path=assessments_path,
    )

    selected = [
        row for row in enriched["rows"] if row["behavioral_factor"]["selected"]
    ]
    assert [row["symbol"] for row in selected] == ["600002", "600003", "600004", "600005"]
    assert all(
        row["behavioral_factor"]["source"] == "behavioral_factor_state"
        for row in enriched["rows"]
    )
    assert enriched["behavioral_factor_summary"] == {
        "model_key": "a_share_behavioral_v1",
        "selection_scope": "factor_model_target_portfolio",
        "rebalance_date": "2026-08-05",
        "candidate_count": 6,
        "selected_count": 4,
        "matched_selected_count": 4,
        "source": "behavioral_factor_state",
        "score_updated_at": "2026-08-05T09:00:00+08:00",
        "target_effective_at": "2026-08-05T10:15:00+08:00",
        "target_effective_status": "active",
        "pending_target_change": False,
    }


def test_snapshot_reports_pending_target_activation_after_five_sessions(tmp_path):
    state_path = tmp_path / "behavioral_factor_state.json"
    assessments_path = tmp_path / "behavioral_factor_assessments.json"
    state_path.write_text(
        json.dumps(
            {
                "selection_scope": "factor_model_target_portfolio",
                "rebalance_date": "2026-08-24",
                "rebalance_sessions": 5,
                "targets": {
                    symbol: {"selected": True, "target_weight_pct": 25.0}
                    for symbol in ("990001", "990002", "990003", "990004")
                },
            }
        ),
        encoding="utf-8",
    )
    assessments_path.write_text(
        json.dumps(
            {
                "date": "2026-08-30T22:00:03+08:00",
                "selection_scope": "factor_model_target_portfolio",
                "assessments": {
                    symbol: {"selected": True, "target_weight_pct": 25.0}
                    for symbol in ("990001", "990002", "990003", "990005")
                },
            }
        ),
        encoding="utf-8",
    )

    enriched = enrich_snapshot_with_behavioral_targets(
        {"version": 1, "rows": []},
        state_path=state_path,
        assessments_path=assessments_path,
    )

    summary = enriched["behavioral_factor_summary"]
    assert summary["score_updated_at"] == "2026-08-30T22:00:03+08:00"
    assert summary["target_effective_at"] == "2026-09-01T09:30+08:00"
    assert summary["target_effective_status"] == "scheduled"
    assert summary["pending_target_change"] is True

    assessments_path.write_text(
        json.dumps(
            {
                "date": "2026-08-30T22:00:03+08:00",
                "selection_scope": "factor_model_target_portfolio",
                "assessments": {
                    symbol: {
                        "selected": True,
                        "target_weight_pct": 25.1 if symbol == "990001" else 25.0,
                    }
                    for symbol in ("990001", "990002", "990003", "990004")
                },
            }
        ),
        encoding="utf-8",
    )

    weight_change = enrich_snapshot_with_behavioral_targets(
        {"version": 1, "rows": []},
        state_path=state_path,
        assessments_path=assessments_path,
    )

    assert weight_change["behavioral_factor_summary"]["pending_target_change"] is True

    refreshed_state = json.loads(state_path.read_text(encoding="utf-8"))
    refreshed_state["score_updated_at"] = "2026-09-01T09:30:00+08:00"
    refreshed_state["targets_effective_at"] = "2026-09-01T09:30:00+08:00"
    state_path.write_text(json.dumps(refreshed_state), encoding="utf-8")

    stale_assessment = enrich_snapshot_with_behavioral_targets(
        {"version": 1, "rows": []},
        state_path=state_path,
        assessments_path=assessments_path,
    )

    stale_summary = stale_assessment["behavioral_factor_summary"]
    assert stale_summary["score_updated_at"] == "2026-09-01T09:30:00+08:00"
    assert stale_summary["target_effective_status"] == "active"
    assert stale_summary["pending_target_change"] is False


def test_snapshot_is_unchanged_when_factor_state_is_missing(tmp_path):
    payload = {"version": 1, "rows": [{"symbol": "600000"}]}

    enriched = enrich_snapshot_with_behavioral_targets(
        payload,
        state_path=tmp_path / "missing.json",
    )

    assert enriched is payload


def test_legacy_state_uses_same_day_file_time_for_scoring_and_activation(tmp_path):
    state_path = tmp_path / "behavioral_factor_state.json"
    assessments_path = tmp_path / "behavioral_factor_assessments.json"
    state_path.write_text(
        json.dumps(
            {
                "selection_scope": "factor_model_target_portfolio",
                "rebalance_date": "2026-08-31",
                "rebalance_sessions": 5,
                "targets": {
                    "990001": {"selected": True, "target_weight_pct": 100.0}
                },
            }
        ),
        encoding="utf-8",
    )
    state_timestamp = datetime.fromisoformat("2026-08-31T10:00:18+08:00").timestamp()
    os.utime(state_path, (state_timestamp, state_timestamp))
    assessments_path.write_text(
        json.dumps(
            {
                "date": "2026-08-30T22:00:03+08:00",
                "selection_scope": "factor_model_target_portfolio",
                "assessments": {
                    "990002": {"selected": True, "target_weight_pct": 100.0}
                },
            }
        ),
        encoding="utf-8",
    )

    enriched = enrich_snapshot_with_behavioral_targets(
        {"version": 1, "rows": []},
        state_path=state_path,
        assessments_path=assessments_path,
    )

    summary = enriched["behavioral_factor_summary"]
    assert summary["score_updated_at"] == "2026-08-31T10:00:18+08:00"
    assert summary["target_effective_at"] == "2026-08-31T10:00:18+08:00"
    assert summary["target_effective_status"] == "active"
    assert summary["pending_target_change"] is False


def test_monitor_snapshot_endpoint_applies_behavioral_enrichment(monkeypatch):
    import core.behavioral_snapshot as factor_snapshot
    import scripts.monitor_window_services as services

    monkeypatch.delenv("INVEST_API_TOKEN", raising=False)
    monkeypatch.setattr(
        services,
        "_load_snapshot",
        lambda _path: {"version": 1, "rows": [{"symbol": "600000"}]},
    )
    monkeypatch.setattr(
        factor_snapshot,
        "enrich_snapshot_with_behavioral_targets",
        lambda payload: {**payload, "behavioral_factor_summary": {"selected_count": 1}},
    )

    with TestClient(app) as client:
        response = client.get("/api/monitor/snapshot")

    assert response.status_code == 200
    assert response.json()["behavioral_factor_summary"]["selected_count"] == 1
