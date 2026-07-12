"""Empirical 10-minute price anomaly sentinel for the desktop monitor.

The sentinel never trades.  It records fresh intraday quotes, estimates
symbol/sector residual-return tails by time-of-day, and asks the already-running
Python committee to review statistically unusual moves.
"""
from __future__ import annotations

import math
import os
import sqlite3
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "market_monitor" / "intraday_price_sentinel.sqlite"
DEFAULT_TAIL_PROBABILITY = float(os.getenv("INVEST_INTRADAY_SENTINEL_TAIL_PROBABILITY", "0.005"))
DEFAULT_MIN_SAMPLES = int(os.getenv("INVEST_INTRADAY_SENTINEL_MIN_SAMPLES", "200"))
DEFAULT_COOLDOWN_MINUTES = int(os.getenv("INVEST_INTRADAY_SENTINEL_COOLDOWN_MINUTES", "30"))
DEFAULT_QUOTE_MAX_AGE_SECONDS = int(os.getenv("INVEST_INTRADAY_QUOTE_MAX_AGE_SECONDS", "180"))


def _time_band(now: datetime) -> Optional[str]:
    minute = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minute < 10 * 60 + 30:
        return "09:30-10:30"
    if 10 * 60 + 30 <= minute <= 11 * 60 + 30:
        return "10:30-11:30"
    if 13 * 60 <= minute < 14 * 60:
        return "13:00-14:00"
    if 14 * 60 <= minute <= 15 * 60:
        return "14:00-15:00"
    return None


def empirical_tail_thresholds(
    values: Iterable[float],
    *,
    tail_probability: float = DEFAULT_TAIL_PROBABILITY,
) -> Tuple[float, float]:
    """Finite-sample two-tail thresholds using conservative order statistics."""
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        raise ValueError("tail thresholds require samples")
    alpha = min(max(float(tail_probability), 1.0 / (len(clean) + 1)), 0.25)
    lower_index = max(0, math.floor((len(clean) + 1) * alpha) - 1)
    upper_index = min(len(clean) - 1, math.ceil((len(clean) + 1) * (1.0 - alpha)) - 1)
    return clean[lower_index], clean[upper_index]


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    except (TypeError, ValueError):
        return None


def _clean_sector(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"", "unknown", "none", "null", "-"} or text in {"未分组", "全局"} else text


class IntradayPriceSentinel:
    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        tail_probability: float = DEFAULT_TAIL_PROBABILITY,
        cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
        quote_max_age_seconds: int = DEFAULT_QUOTE_MAX_AGE_SECONDS,
    ) -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.min_samples = max(20, int(min_samples))
        self.tail_probability = min(max(float(tail_probability), 0.001), 0.10)
        self.cooldown_minutes = max(0, int(cooldown_minutes))
        self.quote_max_age_seconds = max(30, int(quote_max_age_seconds))
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quote_samples (
                    symbol TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    time_band TEXT NOT NULL,
                    sector TEXT,
                    price REAL NOT NULL,
                    quote_source TEXT,
                    quote_fetched_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_quote_samples_symbol_ts
                    ON quote_samples(symbol, ts DESC);
                CREATE TABLE IF NOT EXISTS return_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    time_band TEXT NOT NULL,
                    sector TEXT,
                    interval_minutes REAL NOT NULL,
                    raw_log_return REAL NOT NULL,
                    sector_log_return REAL NOT NULL,
                    residual_log_return REAL NOT NULL,
                    sector_adjusted INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(symbol, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_return_symbol_band_ts
                    ON return_samples(symbol, time_band, ts);
                CREATE INDEX IF NOT EXISTS idx_return_sector_band_ts
                    ON return_samples(sector, time_band, ts);
                CREATE TABLE IF NOT EXISTS sentinel_alerts (
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    last_alert_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, direction)
                );
                PRAGMA user_version=1;
                """
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _unavailable(self, *, band: Optional[str], reason: str, stale: bool = False) -> Dict[str, Any]:
        return {
            "available": False,
            "triggered": False,
            "extreme": False,
            "committee_review_required": False,
            "time_band": band or "",
            "sample_count": 0,
            "model_scope": "",
            "is_stale": bool(stale),
            "reason": reason,
        }

    def _history(self, symbol: str, sector: str, band: str, before: str) -> Tuple[List[float], str]:
        rows = self.conn.execute(
            """SELECT residual_log_return FROM return_samples
               WHERE symbol = ? AND time_band = ? AND ts < ?
               ORDER BY ts DESC LIMIT 5000""",
            (symbol, band, before),
        ).fetchall()
        values = [float(row[0]) for row in rows]
        if len(values) >= self.min_samples:
            return values, "symbol_time_band"
        if sector:
            rows = self.conn.execute(
                """SELECT residual_log_return FROM return_samples
                   WHERE sector = ? AND time_band = ? AND sector_adjusted = 1 AND ts < ?
                   ORDER BY ts DESC LIMIT 10000""",
                (sector, band, before),
            ).fetchall()
            sector_values = [float(row[0]) for row in rows]
            if len(sector_values) >= self.min_samples:
                return sector_values, "sector_time_band"
        return values, "insufficient"

    def _cooldown_active(self, symbol: str, direction: str, now: datetime) -> bool:
        row = self.conn.execute(
            "SELECT last_alert_at FROM sentinel_alerts WHERE symbol = ? AND direction = ?",
            (symbol, direction),
        ).fetchone()
        if row is None or self.cooldown_minutes <= 0:
            return False
        previous = _parse_timestamp(row[0])
        return bool(previous and (now - previous.astimezone(now.tzinfo)).total_seconds() < self.cooldown_minutes * 60)

    def evaluate_round(
        self,
        prices: Dict[str, Dict[str, Any]],
        stocks: List[Dict[str, Any]],
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Dict[str, Any]]:
        now = now or datetime.now().astimezone()
        if now.tzinfo is None:
            now = now.astimezone()
        band = _time_band(now)
        stock_by_symbol = {
            str(stock.get("symbol") or "").strip().upper(): stock
            for stock in stocks
            if stock.get("symbol")
        }
        symbols = sorted(set(stock_by_symbol) | {str(symbol).strip().upper() for symbol in prices})
        if band is None:
            return {symbol: self._unavailable(band=band, reason="outside_trading_session") for symbol in symbols}

        now_iso = now.isoformat(timespec="seconds")
        trade_date = now.date().isoformat()
        statuses: Dict[str, Dict[str, Any]] = {}
        observations: List[Dict[str, Any]] = []
        with self._lock:
            for symbol in symbols:
                quote = prices.get(symbol) or {}
                price = float(quote.get("price") or 0.0)
                fetched_at = _parse_timestamp(quote.get("fetched_at"))
                stale = bool(quote.get("is_stale"))
                if fetched_at is None:
                    statuses[symbol] = self._unavailable(band=band, reason="quote_timestamp_missing", stale=True)
                    continue
                age = (now.astimezone(timezone.utc) - fetched_at.astimezone(timezone.utc)).total_seconds()
                stale = stale or age > self.quote_max_age_seconds or age < -self.quote_max_age_seconds
                if stale or price <= 0:
                    statuses[symbol] = self._unavailable(
                        band=band,
                        reason="stale_quote" if stale else "invalid_price",
                        stale=stale,
                    )
                    continue
                previous = self.conn.execute(
                    """SELECT ts, price FROM quote_samples
                       WHERE symbol = ? AND trade_date = ? AND ts < ?
                       ORDER BY ts DESC LIMIT 1""",
                    (symbol, trade_date, now_iso),
                ).fetchone()
                sector = _clean_sector((stock_by_symbol.get(symbol) or {}).get("sector"))
                self.conn.execute(
                    """INSERT OR IGNORE INTO quote_samples
                       (symbol, ts, trade_date, time_band, sector, price, quote_source, quote_fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        symbol,
                        now_iso,
                        trade_date,
                        band,
                        sector,
                        price,
                        str(quote.get("quote_source") or ""),
                        fetched_at.isoformat(timespec="seconds"),
                    ),
                )
                if previous is None:
                    statuses[symbol] = self._unavailable(band=band, reason="collecting_first_sample")
                    continue
                previous_ts = _parse_timestamp(previous["ts"])
                previous_price = float(previous["price"] or 0.0)
                interval = (now - previous_ts.astimezone(now.tzinfo)).total_seconds() / 60.0 if previous_ts else 0.0
                if previous_price <= 0 or not 5.0 <= interval <= 20.0:
                    statuses[symbol] = self._unavailable(band=band, reason="non_10min_interval")
                    continue
                observations.append(
                    {
                        "symbol": symbol,
                        "sector": sector,
                        "interval": interval,
                        "raw": math.log(price / previous_price),
                    }
                )

            sector_returns: Dict[str, List[float]] = {}
            for observation in observations:
                if observation["sector"]:
                    sector_returns.setdefault(observation["sector"], []).append(observation["raw"])

            for observation in observations:
                symbol = observation["symbol"]
                sector = observation["sector"]
                peer_returns = sector_returns.get(sector, [])
                sector_adjusted = bool(sector and len(peer_returns) >= 3)
                sector_return = statistics.median(peer_returns) if sector_adjusted else 0.0
                residual = observation["raw"] - sector_return
                values, scope = self._history(symbol, sector, band, now_iso)
                self.conn.execute(
                    """INSERT OR IGNORE INTO return_samples
                       (symbol, ts, trade_date, time_band, sector, interval_minutes,
                        raw_log_return, sector_log_return, residual_log_return, sector_adjusted)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        symbol,
                        now_iso,
                        trade_date,
                        band,
                        sector,
                        observation["interval"],
                        observation["raw"],
                        sector_return,
                        residual,
                        int(sector_adjusted),
                    ),
                )
                if len(values) < self.min_samples:
                    statuses[symbol] = {
                        **self._unavailable(band=band, reason="insufficient_empirical_history"),
                        "sample_count": len(values),
                        "model_scope": scope,
                        "current_return_pct": (math.exp(observation["raw"]) - 1.0) * 100.0,
                        "residual_return_pct": (math.exp(residual) - 1.0) * 100.0,
                    }
                    continue
                lower, upper = empirical_tail_thresholds(values, tail_probability=self.tail_probability)
                direction = "up" if residual >= upper and upper > 0 else "down" if residual <= lower and lower < 0 else ""
                extreme = bool(direction)
                cooldown = extreme and self._cooldown_active(symbol, direction, now)
                triggered = extreme and not cooldown
                alert_id = ""
                if triggered:
                    self.conn.execute(
                        """INSERT INTO sentinel_alerts(symbol, direction, last_alert_at)
                           VALUES (?, ?, ?)
                           ON CONFLICT(symbol, direction) DO UPDATE SET last_alert_at=excluded.last_alert_at""",
                        (symbol, direction, now_iso),
                    )
                    alert_id = f"{symbol}:{direction}:{now_iso}"
                rank = sum(1 for value in values if value <= residual)
                statuses[symbol] = {
                    "available": True,
                    "triggered": triggered,
                    "extreme": extreme,
                    "committee_review_required": triggered,
                    "direction": direction,
                    "cooldown_active": bool(cooldown),
                    "alert_id": alert_id,
                    "time_band": band,
                    "sample_count": len(values),
                    "model_scope": scope,
                    "tail_probability": self.tail_probability,
                    "current_return_pct": (math.exp(observation["raw"]) - 1.0) * 100.0,
                    "residual_return_pct": (math.exp(residual) - 1.0) * 100.0,
                    "lower_threshold_pct": (math.exp(lower) - 1.0) * 100.0,
                    "upper_threshold_pct": (math.exp(upper) - 1.0) * 100.0,
                    "empirical_percentile": rank / len(values),
                    "is_stale": False,
                    "reason": "empirical_tail_trigger" if triggered else "direction_cooldown" if cooldown else "within_empirical_band",
                }
            self.conn.commit()
        return statuses
