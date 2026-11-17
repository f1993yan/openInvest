from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import jobs.market_monitor_runtime as runtime


def test_startup_round_waits_before_next_trading_round(monkeypatch):
    run_round = MagicMock(side_effect=[None, KeyboardInterrupt])
    wait_next = MagicMock()

    monkeypatch.setattr(runtime, "is_trading_time", lambda: True)
    monkeypatch.setattr(runtime, "run_monitor_round", run_round)
    monkeypatch.setattr(runtime, "wait_until_next_round", wait_next)

    runtime.main()

    assert run_round.call_count == 2
    wait_next.assert_called_once_with()


def test_monitor_and_news_intervals_are_independent(tmp_path, monkeypatch):
    import jobs.market_monitor_common as common

    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs/crawler_settings.json").write_text(json.dumps({
        "frequency_minutes": 4, "news_frequency_minutes": 10,
    }), encoding="utf-8")
    monkeypatch.setattr(common, "_PROJECT_ROOT", tmp_path)
    assert int(common.INTERVAL_MINUTES) == 4
    assert common.load_crawler_settings()["news_frequency_minutes"] == 10


@pytest.mark.parametrize("stamp,seconds", [
    ("2026-11-03T10:41:10", 170),
    ("2026-11-03T10:48:22", 218),
])
def test_four_minute_schedule_waits_for_next_slot(monkeypatch, stamp, seconds):
    class Clock(datetime):
        @classmethod
        def now(cls):
            return datetime.fromisoformat(stamp)

    sleep = MagicMock()
    monkeypatch.setattr(runtime, "datetime", Clock)
    monkeypatch.setattr(runtime, "INTERVAL_MINUTES", 4)
    monkeypatch.setattr(runtime.time, "sleep", sleep)
    runtime.wait_until_next_round()
    sleep.assert_called_once_with(seconds)


def test_older_client_keeps_separate_news_interval(tmp_path, monkeypatch):
    from backend import server

    (tmp_path / "jobs").mkdir()
    path = tmp_path / "jobs/crawler_settings.json"
    path.write_text(json.dumps({"frequency_minutes": 10, "news_frequency_minutes": 15}), encoding="utf-8")
    monkeypatch.setattr(server, "_PROJECT_ROOT", tmp_path)
    saved = asyncio.run(server.update_crawler_config(server.CrawlerSettings(
        frequency_minutes=4, target_refresh_enabled=True, news_refresh_enabled=True)))
    assert saved["settings"]["frequency_minutes"] == 4
    assert saved["settings"]["news_frequency_minutes"] == 15
    assert asyncio.run(server.get_crawler_config())["news_frequency_minutes"] == 15
