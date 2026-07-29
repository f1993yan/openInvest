from __future__ import annotations

from unittest.mock import MagicMock

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
