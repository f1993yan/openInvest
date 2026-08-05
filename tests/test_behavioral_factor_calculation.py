from __future__ import annotations

import json
from types import SimpleNamespace


def test_weekly_cache_lists_only_selected_top_four(monkeypatch, tmp_path):
    from jobs import behavioral_factor_calculation as job

    stocks = [
        {"symbol": f"60000{i}", "market": "a"}
        for i in range(6)
    ]

    class Ledger:
        def ensure_initialized(self, _config):
            return None

    monkeypatch.setattr(job, "load_config", lambda: {"holdings": stocks, "watchlist": []})
    monkeypatch.setattr(job, "AccountLedger", Ledger)
    monkeypatch.setattr("utils.market_data_provider.get_history_data", lambda *_: SimpleNamespace(empty=False))
    monkeypatch.setattr(
        "core.ashare_behavioral_factor.assess_behavioral_universe",
        lambda _histories: {
            stock["symbol"]: SimpleNamespace(
                selected=index < 4,
                as_dict=lambda symbol=stock["symbol"], selected=index < 4: {
                    "symbol": symbol,
                    "selected": selected,
                },
            )
            for index, stock in enumerate(stocks)
        },
    )
    monkeypatch.setattr(job, "build_behavioral_factor_context", lambda _stocks: {})
    output = tmp_path / "behavioral_factor_assessments.json"
    monkeypatch.setattr(job, "ASSESSMENTS_FILE", output)

    result = job.run()

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result == "Selected 4 of 6 symbols."
    assert payload["candidate_count"] == 6
    assert payload["selected_count"] == 4
    assert len(payload["assessments"]) == 4
    assert all(row["selected"] for row in payload["assessments"].values())
