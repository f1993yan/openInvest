from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.server import app
from core.behavioral_snapshot import enrich_snapshot_with_behavioral_targets


def test_snapshot_uses_persisted_targets_and_caps_selection_at_four(tmp_path):
    state_path = tmp_path / "behavioral_factor_state.json"
    state_path.write_text(
        json.dumps(
            {
                "model": "a_share_behavioral_v1",
                "selection_scope": "factor_model_target_portfolio",
                "rebalance_date": "2026-08-05",
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

    enriched = enrich_snapshot_with_behavioral_targets(payload, state_path=state_path)

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
    }


def test_snapshot_is_unchanged_when_factor_state_is_missing(tmp_path):
    payload = {"version": 1, "rows": [{"symbol": "600000"}]}

    enriched = enrich_snapshot_with_behavioral_targets(
        payload,
        state_path=tmp_path / "missing.json",
    )

    assert enriched is payload


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
