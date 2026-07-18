"""Shared SQLite WAL lifecycle helpers.

The application has several independent SQLite stores. Keeping connection
setup and WAL maintenance in one place prevents a long-running writer from
leaving an unbounded ``-wal`` file while preserving normal WAL concurrency.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

DEFAULT_WAL_TRUNCATE_BYTES = 64 * 1024 * 1024


def configure_wal_connection(conn: sqlite3.Connection) -> None:
    """Apply the common concurrency and durability pragmas."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.commit()


def maintain_wal(
    conn: sqlite3.Connection,
    db_path: str | os.PathLike[str],
    *,
    force_truncate: bool = False,
    truncate_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Checkpoint pages and truncate only an oversized, currently idle WAL."""
    threshold = truncate_bytes
    if threshold is None:
        try:
            threshold = int(
                os.getenv("INVEST_SQLITE_WAL_TRUNCATE_BYTES", str(DEFAULT_WAL_TRUNCATE_BYTES))
            )
        except (TypeError, ValueError):
            threshold = DEFAULT_WAL_TRUNCATE_BYTES
    threshold = max(0, int(threshold))

    wal_path = Path(f"{os.fspath(db_path)}-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    passive = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    busy = int(passive[0]) if passive else 0
    log_frames = int(passive[1]) if passive and len(passive) > 1 else 0
    checkpointed_frames = int(passive[2]) if passive and len(passive) > 2 else 0
    truncated = False

    if busy == 0 and (force_truncate or (threshold > 0 and wal_bytes >= threshold)):
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        truncated = bool(result is not None and int(result[0]) == 0)

    return {
        "busy": busy,
        "log_frames": log_frames,
        "checkpointed_frames": checkpointed_frames,
        "wal_bytes": wal_bytes,
        "truncated": truncated,
    }


def close_wal_connection(
    conn: Optional[sqlite3.Connection],
    db_path: str | os.PathLike[str],
) -> None:
    """Rollback unfinished work, checkpoint best-effort, then close."""
    if conn is None:
        return
    try:
        conn.rollback()
    except sqlite3.Error:
        pass
    try:
        maintain_wal(conn, db_path)
    except sqlite3.Error as exc:
        log.debug("SQLite WAL checkpoint skipped for %s: %s", db_path, exc)
    conn.close()


__all__ = [
    "DEFAULT_WAL_TRUNCATE_BYTES",
    "close_wal_connection",
    "configure_wal_connection",
    "maintain_wal",
]
