from __future__ import annotations

from fastapi.testclient import TestClient

from backend import server as server_module
from backend.server import app


def test_api_token_is_optional(monkeypatch):
    monkeypatch.delenv("INVEST_API_TOKEN", raising=False)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/monitor/snapshot").status_code == 200


def test_api_token_protects_every_non_health_route(monkeypatch):
    monkeypatch.setenv("INVEST_API_TOKEN", "unit-test-token")
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        missing = client.get("/api/monitor/snapshot")
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert client.get(
            "/api/monitor/snapshot",
            headers={"Authorization": "Bearer wrong"},
        ).status_code == 401
        assert client.get(
            "/api/monitor/snapshot",
            headers={"Authorization": "Bearer unit-test-token"},
        ).status_code == 200


def test_behavioral_factor_repair_uses_holdings_and_watchlist(tmp_path, monkeypatch):
    config_path = tmp_path / "market_monitor_config.json"
    config_path.write_text(
        '{"holdings":[{"symbol":"SH600183","market":"sh"}],'
        '"watchlist":[{"symbol":"002185.SZ","market":"sz"},{"symbol":"601138","market":"a"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(server_module, "MONITOR_CONFIG_PATH", config_path)

    captured = {}

    def fake_context(rows):
        captured["symbols"] = {row["symbol"] for row in rows}
        return {
            "600900": {
                "symbol": "600900",
                "score": 80.0,
                "expected_return_pct": 4.0,
                "target_weight_pct": 20.0,
                "eligible": True,
                "selected": True,
                "low_confidence": False,
                "sample_size": 40,
            }
        }

    import jobs.market_monitor_quotes as quotes

    monkeypatch.setattr(quotes, "build_behavioral_factor_context", fake_context)
    assessment = server_module._resolve_a_share_behavioral_assessment(
        "600900",
        holdings=[{"symbol": "600487", "market": "a"}],
    )

    assert assessment.low_confidence is False
    assert assessment.selected is True
    assert captured["symbols"] == {"600900", "600487", "600183", "002185", "601138"}


def test_hk_spatio_factor_repair_uses_only_hk_holdings_and_watchlist(tmp_path, monkeypatch):
    config_path = tmp_path / "market_monitor_config.json"
    config_path.write_text(
        '{"holdings":[{"symbol":"00700","market":"hk"},{"symbol":"600183","market":"a"}],'
        '"watchlist":[{"symbol":"09988.HK","market":"hk"},{"symbol":"00005","market":"hk"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(server_module, "MONITOR_CONFIG_PATH", config_path)
    captured = {}

    def fake_context(rows):
        captured["symbols"] = {row["symbol"] for row in rows}
        return {
            "00941": {
                "symbol": "00941",
                "score": 88.0,
                "expected_return_pct": 5.0,
                "target_weight_pct": 30.0,
                "eligible": True,
                "selected": True,
                "low_confidence": False,
                "sample_size": 40,
            }
        }

    import jobs.market_monitor_quotes as quotes

    monkeypatch.setattr(quotes, "build_hk_spatio_factor_context", fake_context)
    assessment = server_module._resolve_hk_spatio_assessment(
        "00941",
        holdings=[{"symbol": "00001", "market": "hk"}, {"symbol": "600487", "market": "a"}],
    )

    assert assessment.low_confidence is False
    assert assessment.selected is True
    assert captured["symbols"] == {"00941", "00001", "00700", "09988", "00005"}


def test_retired_exit_parameter_endpoints_remain_non_persisting_compatible(monkeypatch):
    monkeypatch.delenv("INVEST_API_TOKEN", raising=False)
    with TestClient(app) as client:
        posted = client.post("/api/config/exit_params", json={"max_loss_pct": 5})
        assert posted.status_code == 200
        assert posted.json()["ok"] is True
        assert client.get("/api/config/exit_params").json() == {}

        policies = client.post("/api/config/env_policies", json={"policies": "sensitive-old-value"})
        assert policies.status_code == 200
        assert policies.json()["ok"] is True
        assert client.get("/api/config/env_policies").json() == {"policies": ""}
