import pandas as pd
import numpy as np
from scripts.monitor_window_text import _beginner_summary_lines, _detail_line_style, _operation_summary, _sector_summary
from scripts.monitor_window_services import _fill_missing_row_sectors

def test_beginner_summary_lines_includes_chan_analysis(monkeypatch):
    # Prepare mock DataFrame with sufficient rows
    dates = pd.date_range("2026-01-01", periods=150, freq="D")
    close = [100.0 + 5.0 * np.sin(i / 5.0) for i in range(150)]
    high = [c + 1.0 for c in close]
    low = [c - 1.0 for c in close]
    open_p = [c - 0.2 for c in close]
    volume = [10000.0 for _ in range(150)]

    fake_df = pd.DataFrame(
        {
            "Open": open_p,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
        },
        index=dates,
    )

    # Monkeypatch the get_history_data function in scripts.monitor_window_text
    import utils.market_data_provider
    monkeypatch.setattr(utils.market_data_provider, "get_history_data", lambda symbol, period="2y": fake_df)

    row = {
        "symbol": "688017",
        "name": "测试标的",
        "price": {"current": 408.01, "change_pct": 6.92},
        "operation": {"verdict": "HOLD", "confidence": 0.8, "suggested_alloc_cny": 0},
    }

    lines = _beginner_summary_lines(row)
    text = "\n".join(lines)

    # Verify that the output text includes the Chan Theory and Pattern Analysis shadow mode header
    assert "--- 缠论与技术形态分析 [影子模式] ---" in text
    assert "最新确认笔" in text


def test_stop_take_and_alloc_fallback(monkeypatch):
    # Mock get_history_data to return empty df so we skip Chan analysis and focus on fallback
    import utils.market_data_provider
    monkeypatch.setattr(utils.market_data_provider, "get_history_data", lambda symbol, period="2y": pd.DataFrame())

    row = {
        "symbol": "688017",
        "name": "测试标的",
        "price": {"current": 408.01, "change_pct": 6.92},
        "exit_points": {"stop_loss_price": 0.0, "take_profit_price": 0.0, "trim_price": 0.0},
        "operation": {
            "verdict": "HOLD",
            "confidence": 0.35,
            "suggested_alloc_cny": 0.0,
        },
    }

    result = {
        "success": True,
        "symbol": "688017",
        "entry_exit_points": {
            "current_price": 408.01,
            "buy_pullback_price": 375.81,
            "buy_breakout_price": 424.11,
            "stop_loss_price": 360.00,
            "take_profit_price": 500.00,
        }
    }

    lines = _beginner_summary_lines(row, result=result)
    text = "\n".join(lines)

    # 1. 验证止损和止盈成功回退到了 entry_exit_points 计算出的 360.00 和 500.00
    assert "止损 360.00" in text
    assert "止盈 500.00" in text

    # 2. 验证仓位建议从 "- 元" 正确变成了 "0 元"
    assert "仓位建议: 0 元" in text


def test_negative_alloc_overrides_hold_display_to_trim(monkeypatch):
    import utils.market_data_provider
    monkeypatch.setattr(utils.market_data_provider, "get_history_data", lambda symbol, period="2y": pd.DataFrame())

    row = {
        "symbol": "09988",
        "name": "阿里巴巴-W",
        "state": "candidate",
        "price": {"current": 97.65, "change_pct": -1.31},
        "operation": {"verdict": "HOLD", "status": "candidate", "confidence": 0.86, "suggested_alloc_cny": -17730},
    }
    result = {
        "success": True,
        "symbol": "09988",
        "verdict": "HOLD",
        "confidence": 0.86,
        "suggested_alloc_cny": -17730,
        "entry_exit_points": {"stop_loss_price": 75.13, "take_profit_price": 108.93},
    }

    assert _operation_summary(row) == "待确认卖"
    text = "\n".join(_beginner_summary_lines(row, result=result))
    assert "待确认卖出" in text
    assert "持有不动" not in text


def test_executed_operation_summary_is_stable():
    row = {
        "symbol": "000063",
        "state": "executed",
        "operation": {"status": "executed", "verdict": "BUY", "suggested_alloc_cny": 6000},
    }

    assert _operation_summary(row) == "已执行"


def test_sector_summary_prefers_panic_guard_sector():
    row = {
        "symbol": "002185",
        "sector": "电子元件",
        "operation": {
            "discipline_review": {
                "sector_panic_guard": {"sector": "半导体", "active": True}
            }
        },
    }

    assert _sector_summary(row) == "板块 半导体（恐慌判定）"


def test_beginner_summary_lines_show_sector(monkeypatch):
    import utils.market_data_provider
    monkeypatch.setattr(utils.market_data_provider, "get_history_data", lambda symbol, period="2y": pd.DataFrame())

    row = {
        "symbol": "002185",
        "name": "华天科技",
        "sector": "半导体",
        "price": {"current": 18.0, "change_pct": -2.1},
        "operation": {"verdict": "HOLD", "confidence": 0.5, "suggested_alloc_cny": 0},
    }

    text = "\n".join(_beginner_summary_lines(row))

    assert "所属板块: 半导体" in text


def test_fill_missing_row_sectors_uses_cache_for_existing_snapshot():
    rows = [{"symbol": "SH600900", "name": "长江电力", "sector": "", "industry": ""}]

    filled = _fill_missing_row_sectors(rows, {"600900": "电力行业"})

    assert filled[0]["sector"] == "电力行业"
    assert filled[0]["industry"] == "电力行业"
    assert filled[0]["sector_source"] == "eastmoney_sector_cache"


def test_low_confidence_boilerplate_is_removed(monkeypatch):
    import utils.market_data_provider
    monkeypatch.setattr(utils.market_data_provider, "get_history_data", lambda symbol, period="2y": pd.DataFrame())

    row = {
        "symbol": "09988",
        "name": "阿里巴巴-W",
        "state": "candidate",
        "price": {"current": 97.65, "change_pct": -1.31},
        "entry_exit_points": {"low_confidence": True},
        "operation": {"verdict": "SELL", "status": "candidate", "confidence": 0.78, "suggested_alloc_cny": -19530},
    }

    text = "\n".join(_beginner_summary_lines(row))

    assert "买卖点模型置信度偏低" not in text


def test_detail_line_style_prioritizes_risk_positive_and_muted_lines():
    assert _detail_line_style("- 负预期回报") == "risk"
    assert _detail_line_style("- 止损 75.13") == "risk"
    assert _detail_line_style("- 右侧趋势闸门: 通过") == "positive"
    assert _detail_line_style("1. 当前价: 97.65") == "muted"
    assert _detail_line_style("下一步:") == "heading"
