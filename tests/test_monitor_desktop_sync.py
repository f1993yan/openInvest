from __future__ import annotations

import tkinter as tk
from unittest.mock import MagicMock

import pytest
import requests

import db.account_ledger as account_ledger_module
import jobs.market_monitor as monitor_module
import scripts.monitor_desktop_window as desktop_module
import scripts.monitor_window_services as services_module
import scripts.monitor_window_trade as trade_module
from scripts.monitor_desktop_window import MonitorWindow
from scripts.monitor_window_widgets import CircleButton


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def local_window(monkeypatch, tmp_path, tk_root):
    root = tk_root
    original_bind = tk.Misc.bind

    def record_bind(widget, sequence=None, func=None, add=None):
        if callable(func):
            widget.__dict__.setdefault("_test_bindings", {})[sequence] = func
        return original_bind(widget, sequence, func, add)

    monkeypatch.setattr(tk.Misc, "bind", record_bind)
    monkeypatch.setattr(desktop_module.tk, "Tk", lambda: root)
    monkeypatch.setattr(desktop_module, "load_dotenv", MagicMock())
    monkeypatch.setattr(MonitorWindow, "_setup_tray_icon", lambda _self: None)
    monkeypatch.setattr(desktop_module.messagebox, "askyesno", MagicMock())
    monkeypatch.setattr(desktop_module.messagebox, "showinfo", MagicMock())
    monkeypatch.setattr(desktop_module.messagebox, "showerror", MagicMock())
    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        MagicMock(side_effect=AssertionError("Desktop account edits must stay local")),
    )
    monkeypatch.setenv("INVEST_REMOTE_SERVER_URL", "https://sync.invalid")
    monkeypatch.setenv("INVEST_REMOTE_API_TOKEN", "unused-test-token")

    ledger = MagicMock()
    ledger.account_summary.return_value = {"cash_cny": 1000.0, "t2_pending_cash_cny": 200.0}
    monkeypatch.setattr(account_ledger_module, "AccountLedger", lambda: ledger)
    monkeypatch.setattr(monitor_module, "load_config", lambda: {})
    window = MonitorWindow(tmp_path / "snapshot.json", demo=True)
    window.refresh = MagicMock()

    try:
        yield window
    finally:
        for child in root.winfo_children():
            child.destroy()

    desktop_module.messagebox.askyesno.assert_not_called()
    desktop_module.messagebox.showinfo.assert_not_called()
    requests.sessions.Session.request.assert_not_called()


def test_desktop_remote_transfer_entry_points_are_removed():
    assert not hasattr(MonitorWindow, "upload_data")
    assert not hasattr(MonitorWindow, "sync_data")
    assert not hasattr(services_module, "_remote_api_headers")
    assert "_remote_api_headers" not in services_module.__all__


def test_toolbar_only_keeps_local_trade_button(local_window):
    window = local_window
    buttons = [child for child in window.selection_bar.winfo_children() if isinstance(child, CircleButton)]
    assert buttons == [window.trade_fab]
    assert buttons[0].text == "+"
    assert not hasattr(window, "upload_fab")
    assert not hasattr(window, "sync_fab")
    assert not hasattr(window, "remote_server_url")
    layout = window.selection_buttons_canvas.pack_info()
    assert layout["fill"] == tk.X
    assert layout["expand"] == 1


def test_trade_button_redraw_still_works_without_remote_buttons(local_window, monkeypatch):
    draw = MagicMock()
    monkeypatch.setattr(local_window.trade_fab, "_draw", draw)
    local_window._redraw_action_fabs()
    draw.assert_called_once_with(local_window.trade_fab.color)


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
@pytest.mark.parametrize("demo", [False, True])
def test_manual_trade_saves_without_upload_prompt(local_window, monkeypatch, direction, demo):
    window = local_window
    window.demo = demo
    save = MagicMock(return_value="Local trade saved")
    monkeypatch.setattr(trade_module, "_execute_user_trade_manual", save)
    parent = tk.Frame(window.root)
    window._create_trade_row(parent, {"symbol": "000001", "name": "Test", "units": 300, "min_lot_size": 100})
    widgets = list(_descendants(parent))
    if direction == "SELL":
        direction_button = next(widget for widget in widgets if isinstance(widget, tk.Label) and widget.cget("textvariable"))
        direction_button._test_bindings["<Button-1>"]()
    execute_button = next(widget for widget in widgets if isinstance(widget, tk.Label) and widget.cget("text") == "执行")

    assert execute_button._test_bindings["<Button-1>"]() == "break"

    if demo:
        save.assert_not_called()
        assert "演示模式" in window.status_text.get()
    else:
        save.assert_called_once_with("000001", direction, 1, 100)
        assert window.status_text.get() == "Local trade saved"
    window.refresh.assert_called_once_with()
    desktop_module.messagebox.showerror.assert_not_called()


def test_manual_trade_failure_still_reports_error_without_refresh(local_window, monkeypatch):
    window = local_window
    window.demo = False
    save = MagicMock(side_effect=ValueError("Local ledger unavailable"))
    monkeypatch.setattr(trade_module, "_execute_user_trade_manual", save)
    parent = tk.Frame(window.root)
    window._create_trade_row(parent, {"symbol": "000001", "name": "Test", "units": 300})
    entry = next(widget for widget in _descendants(parent) if isinstance(widget, tk.Entry))

    assert entry._test_bindings["<Return>"]() == "break"

    save.assert_called_once_with("000001", "BUY", 1, 100)
    assert "Local ledger unavailable" in window.status_text.get()
    window.refresh.assert_not_called()


@pytest.mark.parametrize("demo", [False, True])
def test_cash_correction_saves_without_upload_prompt(local_window, monkeypatch, demo):
    window = local_window
    window.demo = demo
    save = MagicMock()
    monkeypatch.setattr(desktop_module, "_correct_real_cash", save)
    window._open_cash_correction_popover()
    entries = [widget for widget in _descendants(window.cash_popover) if isinstance(widget, tk.Entry)]
    for entry, value in zip(entries, ("1234.50", "678.90")):
        entry.delete(0, tk.END)
        entry.insert(0, value)

    assert entries[0]._test_bindings["<Return>"]() == "break"

    if demo:
        save.assert_not_called()
    else:
        save.assert_called_once_with(1234.5, 678.9)
    assert window.cash_popover is None
    assert window.status_text.get() == "可用现金/待交收现金修正成功"
    window.refresh.assert_called_once_with()
    desktop_module.messagebox.showerror.assert_not_called()


@pytest.mark.parametrize("cash,pending", [("-1", "0"), ("0", "-1"), ("invalid", "0")])
def test_invalid_cash_keeps_editor_open_without_saving(local_window, monkeypatch, cash, pending):
    window = local_window
    window.demo = False
    save = MagicMock()
    monkeypatch.setattr(desktop_module, "_correct_real_cash", save)
    window._open_cash_correction_popover()
    entries = [widget for widget in _descendants(window.cash_popover) if isinstance(widget, tk.Entry)]
    for entry, value in zip(entries, (cash, pending)):
        entry.delete(0, tk.END)
        entry.insert(0, value)
    confirm_button = next(widget for widget in _descendants(window.cash_popover) if isinstance(widget, tk.Label) and widget.cget("text") == "确认")

    assert confirm_button._test_bindings["<Button-1>"]() == "break"

    save.assert_not_called()
    window.refresh.assert_not_called()
    assert window.cash_popover.winfo_exists()
    desktop_module.messagebox.showerror.assert_called_once()
