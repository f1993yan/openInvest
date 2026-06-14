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


def test_daily_stock_selection_writes_latest(monkeypatch, tmp_path):
    from scripts import daily_stock_selection as mod

    class _Result:
        trade_date = "2026-06-12"
        sectors = []
        stocks = []
        news_impact_summary = {"count": 0}

    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    monkeypatch.setattr(mod, "fetch_all", lambda **_: [])
    monkeypatch.setattr(mod, "enrich_news_items", lambda items: items)
    monkeypatch.setattr(mod, "_fetch_sector_fund_flows", lambda: [])
    monkeypatch.setattr(mod, "_fund_flow_items", lambda flows: [])
    monkeypatch.setattr(mod, "_load_candidate_history", lambda items: {})
    monkeypatch.setattr(mod, "_load_candidate_fundamentals", lambda items: {})
    monkeypatch.setattr(mod, "_load_portfolio_cash_cny", lambda: 10000)
    monkeypatch.setattr(mod, "build_daily_selection", lambda *_, **__: _Result())
    monkeypatch.setattr(mod, "result_to_dict", lambda result: {"trade_date": result.trade_date, "stocks": []})

    payload = mod.run_daily_stock_selection(write_file=True)

    assert (tmp_path / "latest.json").exists()
    assert payload["latest_path"].endswith("latest.json")
    assert "selection_2026-06-12_" in payload["output_path"]
    assert payload["cash_constraint_cny"] == 10000
    assert payload["sector_fund_flow_status"]["available"] is False


def test_daily_stock_selection_falls_back_to_monitor_config_cash(monkeypatch, tmp_path):
    from scripts import daily_stock_selection as mod

    config_path = tmp_path / "market_monitor_config.json"
    config_path.write_text('{"cash": 28600}', encoding="utf-8")
    monkeypatch.setattr(mod, "MONITOR_CONFIG_PATH", config_path)

    class _PortfolioManager:
        def cash_amount(self, _currency):
            return 0

    monkeypatch.setattr("core.portfolio_manager.PortfolioManager", _PortfolioManager)

    assert mod._load_portfolio_cash_cny() == 28600


def test_daily_stock_selection_uses_fallback_sector_fund_flow(monkeypatch):
    from scripts import daily_stock_selection as mod

    import pandas as pd

    class _Ak:
        @staticmethod
        def stock_sector_fund_flow_rank(**_kwargs):
            raise KeyError("legacy interface changed")

        @staticmethod
        def stock_fund_flow_industry():
            return pd.DataFrame(
                [
                    {
                        "行业": "贵金属",
                        "行业-涨跌幅": 4.93,
                        "净额": 12.93,
                        "领涨股": "招金黄金",
                        "领涨股-涨跌幅": 10.02,
                    }
                ]
            )

        @staticmethod
        def stock_fund_flow_concept():
            return pd.DataFrame(
                [
                    {
                        "行业": "金属锌",
                        "行业-涨跌幅": 5.47,
                        "净额": 30.39,
                        "领涨股": "新威凌",
                        "领涨股-涨跌幅": 29.95,
                    }
                ]
            )

    monkeypatch.setitem(__import__("sys").modules, "akshare", _Ak)

    flows = mod._fetch_sector_fund_flows(max_sectors=2, max_leaders=1)

    assert [row["sector"] for row in flows] == ["金属锌", "贵金属"]
    assert flows[0]["main_net_inflow_cny"] == 3039000000
    assert flows[0]["fund_flow_source"] == "stock_fund_flow_concept"


def test_scheduled_daily_stock_selection_skips_non_trading_day(monkeypatch):
    from jobs import daily_stock_selection as job

    monkeypatch.setattr(job, "is_trading_day", lambda *_: False)
    monkeypatch.setattr(job, "closed_reason", lambda *_: "XSHG 周末休市")

    result = job.run()

    assert result["status"] == "skipped"
    assert "周末" in result["reason"]


def test_scheduled_daily_stock_selection_runs_on_trading_day(monkeypatch):
    from jobs import daily_stock_selection as job

    monkeypatch.setattr(job, "is_trading_day", lambda *_: True)
    monkeypatch.setattr(
        job,
        "run_daily_stock_selection",
        lambda **kwargs: {"trade_date": "2026-06-12", "stocks": [{"symbol": "601138"}], "output_path": "x", "latest_path": "latest"},
    )

    result = job.run()

    assert result["status"] == "ok"
    assert result["stocks"] == 1
    assert result["latest_path"] == "latest"
