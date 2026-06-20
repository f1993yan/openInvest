from datetime import datetime, timedelta

import pandas as pd

from utils.akshare_data import _apply_period_filter, _cache_is_fresh


def test_akshare_cache_freshness_uses_latest_cached_date():
    fresh = pd.DataFrame(
        {"Close": [1.0] * 10},
        index=pd.to_datetime([(datetime.now() - timedelta(days=i)).date() for i in range(10)][::-1]),
    )
    stale = pd.DataFrame(
        {"Close": [1.0] * 10},
        index=pd.to_datetime([(datetime.now() - timedelta(days=30 + i)).date() for i in range(10)][::-1]),
    )

    assert _cache_is_fresh(fresh)
    assert not _cache_is_fresh(stale)


def test_akshare_period_filter_keeps_requested_window():
    old = datetime.now() - timedelta(days=120)
    recent = datetime.now() - timedelta(days=10)
    df = pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.to_datetime([old, recent]))

    filtered = _apply_period_filter(df, "1mo")

    assert list(filtered["Close"]) == [2.0]

