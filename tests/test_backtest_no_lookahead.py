from __future__ import annotations

from typing import Iterator

import pandas as pd
import pytest


@pytest.fixture
def fake_db_with_future(monkeypatch) -> Iterator[list[bool]]:
    import utils.exchange_fee as ef

    dates = pd.date_range("2024-01-01", "2024-12-31", freq="D")
    fake_df = pd.DataFrame(
        {"Close": [100.0 + i * 0.1 for i in range(len(dates))]},
        index=dates,
    )

    monkeypatch.setattr(ef._STORE, "get_history_df", lambda symbol: fake_df.copy())

    provider_called: list[bool] = []

    def _fail_fetch(*args, **kwargs):
        provider_called.append(True)
        raise RuntimeError("market provider disabled in test")

    monkeypatch.setattr("utils.cn_market_provider.fetch_history", _fail_fetch)
    yield provider_called


def test_no_lookahead_with_as_of_date(fake_db_with_future):
    from utils.exchange_fee import get_history_data

    df = get_history_data("FAKE", as_of_date="2024-05-15")

    assert not df.empty
    assert df.index[-1] <= pd.Timestamp("2024-05-15")
    assert pd.Timestamp("2024-05-16") not in df.index


def test_no_as_of_date_keeps_full_df(fake_db_with_future):
    from utils.exchange_fee import get_history_data

    provider_called = fake_db_with_future
    df = get_history_data("FAKE", as_of_date=None)

    assert df.index[-1] == pd.Timestamp("2024-12-31")
    assert provider_called, "live mode should try refreshing the market provider"


def test_cutoff_inclusive_semantics(fake_db_with_future):
    from utils.exchange_fee import get_history_data

    df = get_history_data("FAKE", as_of_date="2024-06-15")

    assert pd.Timestamp("2024-06-15") in df.index
    assert pd.Timestamp("2024-06-16") not in df.index


def test_backtest_patch_routes_through_as_of_date(monkeypatch):
    import utils.exchange_fee as ef

    calls = []

    def spy(symbol, period="2y", as_of_date=None):
        calls.append({"symbol": symbol, "as_of_date": as_of_date})
        return pd.DataFrame()

    monkeypatch.setattr(ef, "get_history_data", spy)

    decision_date = "2024-03-20"
    ef.get_history_data("NDQ.AX", as_of_date=decision_date)

    assert calls[-1]["as_of_date"] == decision_date


def test_empty_df_passes_through():
    from utils.exchange_fee import _apply_cutoff

    empty = pd.DataFrame()
    assert _apply_cutoff(empty, "2024-05-01").empty


def test_apply_cutoff_with_tz_aware_index():
    from utils.exchange_fee import _apply_cutoff

    dates = pd.date_range("2024-01-01", "2024-01-10", freq="D", tz="America/New_York")
    df = pd.DataFrame({"Close": range(len(dates))}, index=dates)

    result = _apply_cutoff(df, "2024-01-05")

    assert not result.empty
    assert len(result) == 5
    assert result.index[-1].strftime("%Y-%m-%d") == "2024-01-05"


def test_capture_macro_context_uses_decision_date(monkeypatch):
    import core.committee as cm
    import utils.market_data_provider as prov

    calls = []
    fake_df = pd.DataFrame(
        {"Close": [17.5]},
        index=pd.to_datetime(["2024-05-01"]),
    )

    def spy_get_history(symbol, period="2y", as_of_date=None):
        calls.append({"symbol": symbol, "as_of_date": as_of_date})
        return fake_df.copy()

    monkeypatch.setattr(prov, "get_history_data", spy_get_history)

    snapshot = cm._capture_macro_context(as_of_date="2024-05-01")

    assert snapshot["captured_at"] == "2024-05-01"
    assert calls
    assert all(c["as_of_date"] == "2024-05-01" for c in calls)


def test_capture_macro_context_live_mode_uses_now(monkeypatch):
    import core.committee as cm
    import utils.market_data_provider as prov

    monkeypatch.setattr(prov, "get_history_data", lambda *a, **kw: pd.DataFrame())

    snapshot = cm._capture_macro_context()

    assert "T" in snapshot["captured_at"]
    assert len(snapshot["captured_at"]) > 10
