"""Refresh optional news off the monitor's trading path."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


class MonitorNewsCache:
    def __init__(self, fetcher: Callable | None = None):
        self._fetcher = fetcher
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._items: list = []
        self._updated_at: float | None = None

    def get(self, *, enabled: bool = True, max_age_seconds: float = 600) -> list:
        if not enabled:
            return []
        with self._lock:
            age = time.monotonic() - self._updated_at if self._updated_at is not None else float("inf")
            fresh = age <= max_age_seconds
            if not fresh and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._refresh, name="monitor-news", daemon=True)
                self._thread.start()
            return list(self._items) if age <= max(3600, max_age_seconds) else []

    def _refresh(self) -> None:
        try:
            if self._fetcher is None:
                from services.news_sources import fetch_all
                items = fetch_all(domestic=True, max_per_source=8, timeout_sec=20)
            else:
                items = self._fetcher()
            with self._lock:
                self._items = list(items)
                self._updated_at = time.monotonic()
            log.info("Background monitor news refreshed: %s items", len(items))
        except Exception as exc:
            log.warning("Background monitor news failed: %s", type(exc).__name__)
