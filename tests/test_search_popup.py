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
    win._search_online_and_analyze = MagicMock()
    
    win._on_search_return = MonitorWindow._on_search_return.__get__(win, MagicMock)
    win._on_search_return()
    
    win._open_analysis_dialog.assert_not_called()
    win._search_online_and_analyze.assert_called_once_with("000333")
