from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from jobs.market_price_sentinel import IntradayPriceSentinel, empirical_tail_thresholds


def test_empirical_tail_thresholds_use_observed_order_statistics():
    values = [float(i) for i in range(200)]
    lower, upper = empirical_tail_thresholds(values, tail_probability=0.005)
    assert lower == 0.0
    assert upper == 199.0


def test_sentinel_waits_for_empirical_history(tmp_path):
    now = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    sentinel = IntradayPriceSentinel(tmp_path / "sentinel.sqlite", min_samples=20)
    try:
        first = sentinel.evaluate_round(
            {"600000": {"price": 10.0, "fetched_at": now.isoformat(), "quote_source": "test"}},
            [{"symbol": "600000", "sector": "银行"}],
            now=now,
        )
        second_now = now + timedelta(minutes=10)
        second = sentinel.evaluate_round(
            {"600000": {"price": 10.1, "fetched_at": second_now.isoformat(), "quote_source": "test"}},
            [{"symbol": "600000", "sector": "银行"}],
            now=second_now,
        )
    finally:
        sentinel.close()

    assert first["600000"]["reason"] == "collecting_first_sample"
    assert second["600000"]["available"] is False
    assert second["600000"]["reason"] == "insufficient_empirical_history"
    assert second["600000"]["sample_count"] == 0


def test_sentinel_triggers_tail_review_and_directional_cooldown(tmp_path):
    now = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    sentinel = IntradayPriceSentinel(
        tmp_path / "sentinel.sqlite",
        min_samples=20,
        cooldown_minutes=30,
    )
    try:
        for index in range(20):
            value = -0.01 + index * 0.001
            ts = (now - timedelta(days=30 - index)).isoformat(timespec="seconds")
            sentinel.conn.execute(
                """INSERT INTO return_samples
                   (symbol, ts, trade_date, time_band, sector, interval_minutes,
                    raw_log_return, sector_log_return, residual_log_return, sector_adjusted)
                   VALUES ('600000', ?, ?, '09:30-10:30', '银行', 10, ?, 0, ?, 0)""",
                (ts, ts[:10], value, value),
            )
        previous = now - timedelta(minutes=10)
        sentinel.conn.execute(
            """INSERT INTO quote_samples
               (symbol, ts, trade_date, time_band, sector, price, quote_source, quote_fetched_at)
               VALUES ('600000', ?, ?, '09:30-10:30', '银行', 100, 'test', ?)""",
            (previous.isoformat(timespec="seconds"), now.date().isoformat(), previous.isoformat(timespec="seconds")),
        )
        sentinel.conn.commit()

        first = sentinel.evaluate_round(
            {"600000": {"price": 105.0, "fetched_at": now.isoformat(), "quote_source": "test"}},
            [{"symbol": "600000", "sector": "银行"}],
            now=now,
        )["600000"]
        later = now + timedelta(minutes=10)
        second = sentinel.evaluate_round(
            {"600000": {"price": 120.0, "fetched_at": later.isoformat(), "quote_source": "test"}},
            [{"symbol": "600000", "sector": "银行"}],
            now=later,
        )["600000"]
    finally:
        sentinel.close()

    assert first["available"] is True
    assert first["triggered"] is True
    assert first["committee_review_required"] is True
    assert first["direction"] == "up"
    assert first["sample_count"] == 20
    assert first["upper_threshold_pct"] == pytest.approx((math.exp(0.009) - 1) * 100)
    assert second["extreme"] is True
    assert second["triggered"] is False
    assert second["cooldown_active"] is True
    assert second["reason"] == "direction_cooldown"


def test_sentinel_rejects_stale_quote(tmp_path):
    now = datetime.now().astimezone().replace(hour=14, minute=30, second=0, microsecond=0)
    sentinel = IntradayPriceSentinel(tmp_path / "sentinel.sqlite")
    try:
        result = sentinel.evaluate_round(
            {
                "600000": {
                    "price": 10.0,
                    "fetched_at": (now - timedelta(minutes=10)).isoformat(),
                    "quote_source": "cache",
                }
            },
            [{"symbol": "600000", "sector": "银行"}],
            now=now,
        )["600000"]
    finally:
        sentinel.close()

    assert result["available"] is False
    assert result["is_stale"] is True
    assert result["reason"] == "stale_quote"
