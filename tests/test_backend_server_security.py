from __future__ import annotations

from fastapi.testclient import TestClient

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
