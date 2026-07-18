from __future__ import annotations

import sqlite3

from utils.sqlite_lifecycle import (
    close_wal_connection,
    configure_wal_connection,
    maintain_wal,
)


def test_force_checkpoint_truncates_idle_wal(tmp_path):
    path = tmp_path / "store.db"
    conn = sqlite3.connect(path)
    configure_wal_connection(conn)
    conn.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO rows(value) VALUES (?)", [("x" * 200,)] * 200)
    conn.commit()

    result = maintain_wal(conn, path, force_truncate=True)

    assert result["busy"] == 0
    assert result["truncated"] is True
    wal_path = path.with_name(path.name + "-wal")
    assert not wal_path.exists() or wal_path.stat().st_size == 0
    conn.close()


def test_close_rolls_back_unfinished_transaction(tmp_path):
    path = tmp_path / "store.db"
    conn = sqlite3.connect(path)
    configure_wal_connection(conn)
    conn.execute("CREATE TABLE rows (value TEXT)")
    conn.commit()
    conn.execute("INSERT INTO rows(value) VALUES ('uncommitted')")

    close_wal_connection(conn, path)

    with sqlite3.connect(path) as reopened:
        assert reopened.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 0
