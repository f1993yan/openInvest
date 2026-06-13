from __future__ import annotations

from fastapi.testclient import TestClient

from connectors import web_api


def test_daily_stock_selection_endpoint(monkeypatch):
    client = TestClient(web_api.app)
    payload = {
        "trade_date": "2026-06-12",
        "sectors": [{"sector": "AI infrastructure", "heat_score": 80.0}],
        "stocks": [{"symbol": "601138", "score": 82.0}],
        "news_impact_summary": {"count": 1, "positive": 1, "negative": 0},
    }

    def _fake_run(**kwargs):
        assert kwargs["trade_date"] == "2026-06-12"
        assert kwargs["max_news"] == 5
        assert kwargs["max_stocks"] == 3
        assert kwargs["write_file"] is False
        return payload

    monkeypatch.setattr("scripts.daily_stock_selection.run_daily_stock_selection", _fake_run)

    r = client.post(
        "/api/stock_selection/daily",
        json={"trade_date": "2026-06-12", "max_news": 5, "max_stocks": 3},
    )

    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["stocks"][0]["symbol"] == "601138"
    assert body["sectors"][0]["sector"] == "AI infrastructure"
