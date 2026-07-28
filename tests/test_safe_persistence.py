from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from utils.safe_persistence import (
    backup_and_atomic_write_json,
    create_sqlite_export,
    inspect_sqlite,
    restore_sqlite_bytes,
)


def _make_db(path, value: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA user_version=7")
        conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE holdings (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE daily_pnl (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO accounts(value) VALUES (?)", (value,))
        conn.execute("INSERT INTO holdings(value) VALUES (?)", (value,))
        conn.commit()


def test_json_write_creates_checksum_manifest(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"old": true}\n', encoding="utf-8")
    backup = backup_and_atomic_write_json(
        target,
        {"new": True},
        reason="test-json",
        backup_root=tmp_path / "backups",
    )

    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}
    manifest = json.loads((tmp_path / "backups" / backup["backup_id"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["entries"][0]["sha256"]
    assert manifest["entries"][0]["size"] > 0


def test_sqlite_export_is_consistent_and_preserves_schema_version(tmp_path):
    source = tmp_path / "source.db"
    _make_db(source, "source")

    exported = create_sqlite_export(
        source,
        required_tables=("accounts", "holdings", "trades", "daily_pnl"),
        export_root=tmp_path / "exports",
    )
    details = inspect_sqlite(exported, required_tables=("accounts", "holdings"))
    assert details["quick_check"] == "ok"
    assert details["user_version"] == 7
    with closing(sqlite3.connect(exported)) as conn:
        assert conn.execute("SELECT value FROM accounts").fetchone()[0] == "source"


def test_restore_rejects_invalid_or_empty_sqlite(tmp_path):
    target = tmp_path / "target.db"
    _make_db(target, "old")

    with pytest.raises(ValueError, match="Invalid SQLite header|missing or empty"):
        restore_sqlite_bytes(
            target,
            b"not-a-database",
            reason="invalid",
            required_tables=("accounts", "holdings"),
            nonempty_tables=("accounts", "holdings"),
            backup_root=tmp_path / "backups",
        )
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT value FROM accounts").fetchone()[0] == "old"

    empty = tmp_path / "empty.db"
    with closing(sqlite3.connect(empty)) as conn:
        conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE holdings (id INTEGER PRIMARY KEY)")
    with pytest.raises(ValueError, match="table is empty"):
        restore_sqlite_bytes(
            target,
            empty.read_bytes(),
            reason="empty",
            required_tables=("accounts", "holdings"),
            nonempty_tables=("accounts", "holdings"),
            backup_root=tmp_path / "backups",
        )


def test_valid_sqlite_restore_keeps_pre_restore_backup(tmp_path):
    target = tmp_path / "target.db"
    incoming = tmp_path / "incoming.db"
    _make_db(target, "old")
    _make_db(incoming, "new")
    Path(f"{target}-wal").write_bytes(b"stale-wal")
    Path(f"{target}-shm").write_bytes(b"stale-shm")
    export = create_sqlite_export(incoming, export_root=tmp_path / "exports")

    result = restore_sqlite_bytes(
        target,
        export.read_bytes(),
        reason="valid",
        required_tables=("accounts", "holdings", "trades", "daily_pnl"),
        nonempty_tables=("accounts", "holdings"),
        backup_root=tmp_path / "backups",
    )

    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT value FROM accounts").fetchone()[0] == "new"
    assert not Path(f"{target}-wal").exists()
    assert not Path(f"{target}-shm").exists()
    backup_db = tmp_path / "backups" / result["backup_id"] / "target.db"
    with closing(sqlite3.connect(backup_db)) as conn:
        assert conn.execute("SELECT value FROM accounts").fetchone()[0] == "old"


def test_live_sqlite_restore_works_while_target_wal_is_open(tmp_path):
    target = tmp_path / "target.db"
    incoming = tmp_path / "incoming.db"
    _make_db(target, "old")
    _make_db(incoming, "new")
    export = create_sqlite_export(incoming, export_root=tmp_path / "exports")

    with closing(sqlite3.connect(target)) as held:
        held.execute("PRAGMA journal_mode=WAL")
        assert held.execute("SELECT value FROM accounts").fetchone()[0] == "old"
        assert Path(f"{target}-wal").exists()

        result = restore_sqlite_bytes(
            target,
            export.read_bytes(),
            reason="live-valid",
            required_tables=("accounts", "holdings", "trades", "daily_pnl"),
            nonempty_tables=("accounts", "holdings"),
            live=True,
            backup_root=tmp_path / "backups",
        )

        assert result["restore_method"] == "sqlite_backup"
        assert held.execute("SELECT value FROM accounts").fetchone()[0] == "new"

    backup_db = tmp_path / "backups" / result["backup_id"] / "target.db"
    with closing(sqlite3.connect(backup_db)) as conn:
        assert conn.execute("SELECT value FROM accounts").fetchone()[0] == "old"
    assert not list(tmp_path.glob(".target.db.*.incoming*"))
