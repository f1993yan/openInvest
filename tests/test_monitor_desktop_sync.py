from __future__ import annotations

from unittest.mock import MagicMock

import requests

import db.account_ledger as account_ledger_module
import scripts.monitor_desktop_window as desktop_module
from scripts.monitor_desktop_window import MonitorWindow


class _Response:
    status_code = 404
    text = ""


class _Ledger:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


def test_remote_sync_does_not_stop_direct_python_workers(monkeypatch):
    window = MagicMock()
    window.remote_server_url = "https://sync.invalid"
    stop_services = MagicMock(side_effect=AssertionError("sync must not stop monitor workers"))

    monkeypatch.setattr(desktop_module.messagebox, "askyesno", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(desktop_module.messagebox, "showinfo", MagicMock())
    monkeypatch.setattr(desktop_module, "_stop_background_services", stop_services)
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: _Response())
    monkeypatch.setattr(account_ledger_module, "AccountLedger", _Ledger)

    MonitorWindow.sync_data(window)

    stop_services.assert_not_called()
    window.refresh.assert_called_once_with()
