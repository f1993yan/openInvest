from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

from scripts.monitor_desktop_window import MonitorWindow
from scripts.monitor_window_services import (
    clear_symbol_reminder_ignore,
    ignore_symbol_for_window,
    is_symbol_reminder_ignored,
    load_reminder_ignore_state,
    reminder_ignore_remaining_seconds,
)


def test_reminder_ignore_persists_for_one_hour_and_expires(tmp_path):
    path = tmp_path / "reminder_ignore_state.json"

    state = ignore_symbol_for_window(" sh600519 ", now=1_000, path=path)

    assert state == {"SH600519": 4_600.0}
    assert is_symbol_reminder_ignored("sh600519", now=4_599, path=path)
    assert reminder_ignore_remaining_seconds("SH600519", now=4_599, path=path) == 1
    assert not is_symbol_reminder_ignored("SH600519", now=4_600, path=path)
    assert load_reminder_ignore_state(path, now=4_600) == {}


def test_reminder_ignore_state_is_atomic_json_and_clear_keeps_other_symbols(tmp_path):
    path = tmp_path / "reminder_ignore_state.json"
    state = ignore_symbol_for_window("600519", now=1_000, path=path)
    state = ignore_symbol_for_window("09988", now=1_000, path=path, state=state)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["ignored_until"]) == {"600519", "09988"}

    state = clear_symbol_reminder_ignore("600519", now=1_001, path=path, state=state)

    assert state == {"09988": 4_600.0}
    assert is_symbol_reminder_ignored("09988", now=1_001, path=path)
    assert not is_symbol_reminder_ignored("600519", now=1_001, path=path)


def test_corrupt_reminder_ignore_state_fails_open(tmp_path):
    path = tmp_path / "reminder_ignore_state.json"
    path.write_text("not-json", encoding="utf-8")

    assert load_reminder_ignore_state(path, now=1_000) == {}
    assert not is_symbol_reminder_ignored("600519", now=1_000, path=path)


def test_maybe_alert_skips_ignored_symbol_but_alerts_other_symbols():
    window = MagicMock()
    window.reminder_ignore_state = {"600519": datetime.now().timestamp() + 3_600}
    window.previous_action_symbols = set()
    window.shake_enabled = True
    window._active_reminder_ignore_state = MonitorWindow._active_reminder_ignore_state.__get__(window, MagicMock)

    rows = [
        {"symbol": "600519", "state": "action_required"},
        {"symbol": "000001", "state": "action_required"},
    ]
    MonitorWindow._maybe_alert(window, rows)

    assert window.previous_action_symbols == {"action:000001"}
    window._shake.assert_called_once_with()
    window._beep.assert_called_once_with()


def test_maybe_alert_retriggers_after_ignore_expiry():
    window = MagicMock()
    window.reminder_ignore_state = {"600519": datetime.now().timestamp() + 3_600}
    window.previous_action_symbols = {"action:600519"}
    window.shake_enabled = True
    window._active_reminder_ignore_state = MonitorWindow._active_reminder_ignore_state.__get__(window, MagicMock)

    MonitorWindow._maybe_alert(
        window,
        [{"symbol": "600519", "state": "action_required"}],
    )

    # The test uses the real clock, so force the state to an already expired
    # deadline before checking the next pass.
    window.reminder_ignore_state = {"600519": 0.0}
    window._shake.reset_mock()
    window._beep.reset_mock()
    MonitorWindow._maybe_alert(
        window,
        [{"symbol": "600519", "state": "action_required"}],
    )

    assert window.previous_action_symbols == {"action:600519"}
    window._shake.assert_called_once_with()
    window._beep.assert_called_once_with()
