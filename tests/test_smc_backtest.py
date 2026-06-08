from __future__ import annotations

import pandas as pd

from core.smc_backtest import (
    SMCBacktestConfig,
    add_smc_features,
    backtest_smc_strategy,
    generate_smc_signals,
)


def _sample_ohlc() -> pd.DataFrame:
    rows = [
        (10.0, 10.4, 9.8, 10.1),
        (10.1, 10.5, 9.9, 10.3),
        (10.3, 10.8, 10.0, 10.7),
        (10.7, 11.0, 10.4, 10.6),
        (10.6, 10.9, 10.2, 10.4),
        (10.4, 10.6, 9.9, 10.0),
        (10.0, 10.2, 9.5, 9.7),
        (9.7, 9.9, 9.1, 9.3),
        (9.3, 9.6, 8.9, 9.4),
        (9.4, 10.0, 9.2, 9.8),
        (9.8, 10.8, 9.7, 10.7),
        (10.7, 11.4, 10.6, 11.2),
        (11.2, 12.0, 11.1, 11.8),
        (11.8, 12.4, 11.7, 12.2),
        (12.2, 12.8, 12.0, 12.6),
        (12.6, 12.9, 12.1, 12.3),
        (12.3, 12.5, 11.8, 12.0),
        (12.0, 12.2, 11.6, 11.7),
        (11.7, 12.6, 11.5, 12.4),
        (12.4, 13.2, 12.3, 13.0),
        (13.0, 13.7, 12.9, 13.5),
        (13.5, 14.0, 13.4, 13.9),
    ]
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)


def test_add_smc_features_marks_breaks_and_fvgs():
    df = _sample_ohlc()
    out = add_smc_features(df, SMCBacktestConfig(swing_lookback=2, atr_window=3))

    assert {"swing_high", "swing_low", "bullish_bos", "bearish_bos", "bullish_fvg"}.issubset(out.columns)
    assert out["swing_high"].any()
    assert out["swing_low"].any()
    assert out["bullish_bos"].any()
    assert out["bullish_fvg"].any()


def test_generate_smc_signals_returns_structured_events():
    signals = generate_smc_signals(_sample_ohlc(), SMCBacktestConfig(swing_lookback=2, atr_window=3))

    assert any(s.kind == "BOS" and s.direction == "long" for s in signals)
    assert all(s.date and s.price > 0 for s in signals)


def test_backtest_smc_strategy_outputs_metrics_and_trades():
    result = backtest_smc_strategy(
        _sample_ohlc(),
        SMCBacktestConfig(
            swing_lookback=2,
            atr_window=3,
            initial_cash=100_000,
            risk_per_trade_pct=1.0,
            reward_risk=1.5,
            max_hold_bars=5,
        ),
    )

    metrics = result["metrics"]
    assert metrics["initial_cash"] == 100_000
    assert metrics["trade_count"] >= 1
    assert "total_return_pct" in metrics
    assert result["equity_curve"]
    assert result["signals"]
    first = result["trades"][0]
    assert first["entry_date"] <= first["exit_date"]
    assert first["direction"] in ("long", "short")


def test_backtest_can_require_fvg_without_crashing():
    result = backtest_smc_strategy(
        _sample_ohlc(),
        SMCBacktestConfig(swing_lookback=2, atr_window=3, require_fvg=True),
    )

    assert "metrics" in result
    assert isinstance(result["trades"], list)
