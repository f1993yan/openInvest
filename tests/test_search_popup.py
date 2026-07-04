from unittest.mock import MagicMock, patch
import json
import sys
import types
import pytest
import tkinter as tk
from scripts.monitor_desktop_window import MonitorWindow

def test_resolve_query_to_code_exact(monkeypatch):
    # Mock fetch_sina_prices
    mock_prices = {
        "600519": {"name": "贵州茅台", "price": 1700.0, "change_pct": 0.5}
    }
    monkeypatch.setattr("utils.market_data_provider.fetch_prices", lambda symbols: mock_prices)

    win = MagicMock()
    win._resolve_query_to_stock = MonitorWindow._resolve_query_to_stock.__get__(win, MagicMock)

    # Test digit code lookup
    resolved = win._resolve_query_to_stock("600519")
    assert resolved is not None
    symbol, name, price_info = resolved
    assert symbol == "600519"
    assert name == "贵州茅台"
    assert price_info["price"] == 1700.0

def test_resolve_query_to_name_akshare(monkeypatch):
    # Mock akshare DataFrame
    import pandas as pd
    fake_df = pd.DataFrame([
        {"code": "000333", "name": "美的集团"}
    ])
    
    class FakeAk:
        @staticmethod
        def stock_info_a_code_name():
            return fake_df
            
    import sys
    monkeypatch.setitem(sys.modules, "akshare", FakeAk)
    
    # Mock fetch_sina_prices
    mock_prices = {
        "000333": {"name": "美的集团", "price": 70.0, "change_pct": -1.2}
    }
    monkeypatch.setattr("utils.market_data_provider.fetch_prices", lambda symbols: mock_prices)

    win = MagicMock()
    win._resolve_query_to_stock = MonitorWindow._resolve_query_to_stock.__get__(win, MagicMock)

    # Test name lookup
    resolved = win._resolve_query_to_stock("美的集团")
    assert resolved is not None
    symbol, name, price_info = resolved
    assert symbol == "000333"
    assert name == "美的集团"
    assert price_info["price"] == 70.0

def test_on_search_return_existing_row(monkeypatch):
    win = MagicMock()
    win.filter_text = MagicMock()
    win.filter_text.get.return_value = "600519"
    win.current_rows = [
        {"symbol": "600519", "name": "贵州茅台"}
    ]
    win._open_analysis_dialog = MagicMock()
    win._search_online_and_analyze = MagicMock()
    
    win._on_search_return = MonitorWindow._on_search_return.__get__(win, MagicMock)
    win._on_search_return()
    
    win._open_analysis_dialog.assert_called_once_with("600519")
    win._search_online_and_analyze.assert_not_called()

def test_on_search_return_non_existing(monkeypatch):
    win = MagicMock()
    win.filter_text = MagicMock()
    win.filter_text.get.return_value = "000333"
    win.current_rows = [
        {"symbol": "600519", "name": "贵州茅台"}
    ]
    win._open_analysis_dialog = MagicMock()
    win.dialogs = {}
    
    win._on_search_return = MonitorWindow._on_search_return.__get__(win, MagicMock)
    win._on_search_return()
    
    win._open_analysis_dialog.assert_called_once()
    called_args = win._open_analysis_dialog.call_args[0]
    assert called_args[0] == "000333"
    assert called_args[1]["_is_resolving"] is True


def test_open_analysis_dialog_adds_watchlist_button(monkeypatch):
    import pandas as pd
    import utils.market_data_provider
    monkeypatch.setattr(utils.market_data_provider, "get_history_data", lambda symbol, period="2y": pd.DataFrame())

    from scripts.monitor_window_analysis import MonitorAnalysisMixin
    
    class DummyWindow(MonitorAnalysisMixin):
        def __init__(self):
            self.root = tk.Tk()
            self.current_rows = []
            self.dialogs = {}
            self.analysis_queue = MagicMock()
            self.status_text = MagicMock()
            
        def _current_watchlist_symbols(self):
            return {"600519"}
            
        def _add_selection_to_watchlist(self, stock):
            return "added"
            
        def _close_dialog(self, symbol, dialog):
            dialog.destroy()
            
    win = DummyWindow()
    
    row_in = {
        "symbol": "600519",
        "name": "贵州茅台",
        "price": {"current": 1700.0, "change_pct": 0.5},
        "operation": {"verdict": "HOLD"},
    }
    
    with patch("threading.Thread") as mock_thread:
        win._open_analysis_dialog("600519", row_in)
        dialog = win.dialogs["600519"]
        assert dialog is not None
        
        labels = []
        def find_labels(widget):
            if isinstance(widget, tk.Label):
                labels.append(widget)
            for child in widget.winfo_children():
                find_labels(child)
        find_labels(dialog)
        
        button_texts = [l.cget("text") for l in labels]
        assert "加入关注" not in button_texts
        dialog.destroy()

    row_out = {
        "symbol": "000333",
        "name": "美的集团",
        "price": {"current": 70.0, "change_pct": 1.2},
        "operation": {"verdict": "HOLD"},
    }
    
    with patch("threading.Thread") as mock_thread:
        win._open_analysis_dialog("000333", row_out)
        dialog = win.dialogs["000333"]
        assert dialog is not None
        
        labels = []
        find_labels(dialog)
        button_texts = [l.cget("text") for l in labels]
        assert "加入关注" in button_texts
        
        btn_widget = next(l for l in labels if l.cget("text") == "加入关注")
        btn_widget._click_handler()
        assert btn_widget.cget("text") == "已关注"
        dialog.destroy()
        
    win.root.destroy()


def test_resolve_and_analyze_worker_success(monkeypatch):
    from scripts.monitor_window_analysis import MonitorAnalysisMixin
    
    class DummyWindow(MonitorAnalysisMixin):
        def __init__(self):
            self.root = MagicMock()
            self.dialogs = {}
            self.analysis_queue = MagicMock()
            self.status_text = MagicMock()
            
        def _resolve_query_to_stock(self, query):
            return "000333", "美的集团", {"price": 70.0, "change_pct": 1.2}
            
        def _build_dialog_summary(self, parent, symbol, row):
            pass
            
        def _analysis_worker(self, symbol, row):
            pass

    win = DummyWindow()
    dialog = MagicMock()
    dialog.winfo_exists.return_value = True
    win.dialogs["美的集团"] = dialog
    
    progress_wrap = MagicMock()
    progress = MagicMock()
    text_box = MagicMock()
    title_label = MagicMock()
    status_label = MagicMock()
    body = MagicMock()
    summary_container = MagicMock()
    progress_label = MagicMock()
    
    with patch("threading.Thread") as mock_thread:
        win._resolve_and_analyze_worker(
            "美的集团",
            dialog,
            progress_wrap,
            progress,
            text_box,
            title_label,
            status_label,
            body,
            summary_container,
            progress_label,
        )
        
        win.root.after.assert_called_once()
        on_success = win.root.after.call_args[0][1]
        
        on_success()
        
        assert win.dialogs["000333"] == dialog
        assert dialog._row["symbol"] == "000333"
        dialog.title.assert_called_with("美的集团 000333 最新委员会分析")
        title_label.configure.assert_called_with(text="美的集团  000333")


def test_dialog_row_from_committee_result_refreshes_negative_alloc_as_sell():
    from scripts.monitor_window_analysis import _dialog_row_from_committee_result

    row = {
        "symbol": "09988",
        "name": "阿里巴巴-W",
        "state": "monitoring",
        "operation": {"verdict": "HOLD", "status": "monitoring", "suggested_alloc_cny": 0},
        "price": {"current": 97.65},
    }
    result = {
        "success": True,
        "verdict": "HOLD",
        "confidence": 0.86,
        "suggested_alloc_cny": -17730,
    }

    refreshed = _dialog_row_from_committee_result(row, result)

    assert refreshed["state"] == "candidate"
    assert refreshed["operation"]["status"] == "candidate"
    assert refreshed["operation"]["verdict"] == "TRIM"
    assert refreshed["operation"]["suggested_alloc_cny"] == -17730


def test_dialog_row_from_committee_result_promotes_latest_sell_to_action_required():
    from scripts.monitor_window_analysis import _dialog_row_from_committee_result
    from scripts.monitor_window_text import _operation_summary

    row = {
        "symbol": "09988",
        "name": "阿里巴巴-W",
        "state": "candidate",
        "is_holding": True,
        "units": 200,
        "position_pct": 20.0,
        "min_lot_size": 100,
        "operation": {"verdict": "TRIM", "status": "candidate", "suggested_alloc_cny": -10000},
        "price": {"current": 97.65},
    }
    result = {
        "success": True,
        "verdict": "SELL",
        "confidence": 0.78,
        "suggested_alloc_cny": -19530,
    }

    refreshed = _dialog_row_from_committee_result(row, result)

    assert refreshed["state"] == "action_required"
    assert refreshed["operation"]["status"] == "action_required"
    assert refreshed["operation"]["verdict"] == "SELL"
    assert _operation_summary(refreshed) == "卖2手"


def test_dialog_row_from_committee_result_keeps_latest_trim_as_candidate():
    from scripts.monitor_window_analysis import _dialog_row_from_committee_result

    row = {
        "symbol": "09988",
        "name": "阿里巴巴-W",
        "state": "candidate",
        "is_holding": True,
        "units": 200,
        "position_pct": 20.0,
        "operation": {"verdict": "TRIM", "status": "candidate", "suggested_alloc_cny": -10000},
        "price": {"current": 97.65},
    }
    result = {
        "success": True,
        "verdict": "TRIM",
        "confidence": 0.62,
        "suggested_alloc_cny": -5000,
    }

    refreshed = _dialog_row_from_committee_result(row, result)

    assert refreshed["state"] == "candidate"
    assert refreshed["operation"]["status"] == "candidate"


def test_merge_snapshot_row_recounts_and_sorts_action_first():
    from scripts.monitor_window_services import _merge_snapshot_row

    payload = {
        "version": 1,
        "generated_at": "2026-06-01T10:00:00",
        "round_time": "old",
        "counts": {"symbols": 2, "action_required": 0, "errors": 0},
        "rows": [
            {"symbol": "600900", "name": "长江电力", "state": "monitoring", "operation": {"verdict": "HOLD"}},
            {"symbol": "09988", "name": "阿里巴巴-W", "state": "candidate", "operation": {"verdict": "TRIM", "suggested_alloc_cny": -10000}},
        ],
    }
    updated_row = {
        "symbol": "09988",
        "name": "阿里巴巴-W",
        "state": "action_required",
        "operation": {"verdict": "SELL", "status": "action_required", "suggested_alloc_cny": -19530},
    }

    merged = _merge_snapshot_row(payload, updated_row)

    assert merged["round_time"] == "manual_committee"
    assert merged["counts"]["symbols"] == 2
    assert merged["counts"]["action_required"] == 1
    assert merged["rows"][0]["symbol"] == "09988"
    assert merged["rows"][0]["state"] == "action_required"


def test_apply_committee_row_update_writes_snapshot_and_refreshes(monkeypatch):
    import scripts.monitor_desktop_window as mod

    win = MagicMock()
    win.demo = False
    win.snapshot_path = "snapshot.json"
    win._watched_file_signature.return_value = ("sig",)
    win.last_payload_signature = ("old",)
    win.watched_mtime_signature = ("old",)

    monkeypatch.setattr(mod, "_update_snapshot_row", MagicMock(return_value={}))

    MonitorWindow._apply_committee_row_update(win, {"symbol": "09988", "state": "action_required"})

    mod._update_snapshot_row.assert_called_once()
    win.refresh.assert_called_once()
    assert win.watched_mtime_signature == ("sig",)
    assert win.last_payload_signature == ()


def test_set_trading_mode_preserves_existing_snapshot_rows(tmp_path, monkeypatch):
    import scripts.monitor_window_services as svc

    config_path = tmp_path / "market_monitor_config.json"
    snapshot_path = tmp_path / "latest_window.json"
    config = {"total_assets": 100000, "cash": 10000, "trading_mode": "active_profit", "holdings": [], "watchlist": []}
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    snapshot_path.write_text(
        json.dumps(
            {
                "version": 1,
                "trading_mode": {"mode": "active_profit", "label": "主动盈利"},
                "rows": [{"symbol": "000063", "state": "action_required"}],
                "counts": {"symbols": 1, "action_required": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    fake_market_monitor = types.SimpleNamespace(
        CONFIG_PATH=config_path,
        load_config=lambda: json.loads(config_path.read_text(encoding="utf-8")),
    )
    monkeypatch.setitem(sys.modules, "jobs.market_monitor", fake_market_monitor)
    monkeypatch.setattr(svc, "ROOT", tmp_path)
    (tmp_path / "data" / "market_monitor").mkdir(parents=True)
    target_snapshot = tmp_path / "data" / "market_monitor" / "latest_window.json"
    target_snapshot.write_text(snapshot_path.read_text(encoding="utf-8"), encoding="utf-8")

    payload = svc._set_trading_mode("risk_off")

    assert payload == {"mode": "risk_off", "label": "主动避险"}
    updated_config = json.loads(config_path.read_text(encoding="utf-8"))
    updated_snapshot = json.loads(target_snapshot.read_text(encoding="utf-8"))
    assert updated_config["trading_mode"] == "risk_off"
    assert updated_snapshot["trading_mode"] == {"mode": "risk_off", "label": "主动避险"}
    assert updated_snapshot["rows"] == [{"symbol": "000063", "state": "action_required"}]


def test_set_action_email_enabled_preserves_existing_snapshot_rows(tmp_path, monkeypatch):
    import scripts.monitor_window_services as svc

    config_path = tmp_path / "market_monitor_config.json"
    config = {"total_assets": 100000, "cash": 10000, "holdings": [], "watchlist": []}
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    fake_market_monitor = types.SimpleNamespace(
        CONFIG_PATH=config_path,
        load_config=lambda: json.loads(config_path.read_text(encoding="utf-8")),
    )
    monkeypatch.setitem(sys.modules, "jobs.market_monitor", fake_market_monitor)
    monkeypatch.setattr(svc, "ROOT", tmp_path)
    target_snapshot = tmp_path / "data" / "market_monitor" / "latest_window.json"
    target_snapshot.parent.mkdir(parents=True)
    target_snapshot.write_text(
        json.dumps(
            {
                "version": 1,
                "rows": [{"symbol": "000063", "state": "action_required"}],
                "counts": {"symbols": 1, "action_required": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    enabled = svc._set_action_email_enabled(False)

    assert enabled is False
    updated_config = json.loads(config_path.read_text(encoding="utf-8"))
    updated_snapshot = json.loads(target_snapshot.read_text(encoding="utf-8"))
    assert updated_config["monitor_action_email_enabled"] is False
    assert updated_snapshot["monitor_action_email_enabled"] is False
    assert updated_snapshot["rows"] == [{"symbol": "000063", "state": "action_required"}]


def test_toggle_action_email_enabled_updates_status_and_refresh(monkeypatch):
    import scripts.monitor_desktop_window as mod

    win = MagicMock()
    win.demo = False
    win.action_email_enabled = True
    win.action_email_btn = MagicMock()
    win.status_text = MagicMock()
    win.last_payload_signature = ("old",)
    win.refresh = MagicMock()

    monkeypatch.setattr(mod, "_set_action_email_enabled", MagicMock(return_value=False))
    win._render_action_email_button = MonitorWindow._render_action_email_button.__get__(win, MagicMock)

    MonitorWindow._toggle_action_email_enabled(win)

    mod._set_action_email_enabled.assert_called_once_with(False)
    win.action_email_btn.configure.assert_called_once()
    win.status_text.set.assert_called_once_with("执行邮件: 关")
    assert win.last_payload_signature == ()
    win.refresh.assert_called_once()
