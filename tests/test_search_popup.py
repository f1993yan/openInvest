from unittest.mock import MagicMock, patch
import pytest
import tkinter as tk
from scripts.monitor_desktop_window import MonitorWindow

def test_resolve_query_to_code_exact(monkeypatch):
    # Mock fetch_sina_prices
    mock_prices = {
        "600519": {"name": "贵州茅台", "price": 1700.0, "change_pct": 0.5}
    }
    monkeypatch.setattr("jobs.market_monitor_quotes.fetch_sina_prices", lambda symbols: mock_prices)

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
            
    monkeypatch.setattr("sys.modules", {"akshare": FakeAk})
    
    # Mock fetch_sina_prices
    mock_prices = {
        "000333": {"name": "美的集团", "price": 70.0, "change_pct": -1.2}
    }
    monkeypatch.setattr("jobs.market_monitor_quotes.fetch_sina_prices", lambda symbols: mock_prices)

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


def test_open_analysis_dialog_adds_watchlist_button():
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


