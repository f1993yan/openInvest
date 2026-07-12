"""Atomic local persistence, backups, and SQLite restore validation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_ROOT = ROOT / "data" / "backups"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(Path(path), content.encode(encoding))


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        Path(path),
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def sqlite_online_backup(source: Path, destination: Path) -> None:
    """Create a transactionally consistent SQLite copy, including WAL pages."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    uri = f"{source.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=10.0)) as src:
            with closing(sqlite3.connect(tmp, timeout=10.0)) as dst:
                src.backup(dst)
                dst.commit()
        os.replace(tmp, destination)
    finally:
        if tmp.exists():
            tmp.unlink()


def inspect_sqlite(
    path: Path,
    *,
    required_tables: Sequence[str] = (),
    nonempty_tables: Sequence[str] = (),
) -> Dict[str, Any]:
    candidate = Path(path)
    if not candidate.exists() or candidate.stat().st_size < 100:
        raise ValueError("SQLite file is missing or empty")
    with candidate.open("rb") as handle:
        if handle.read(16) != b"SQLite format 3\x00":
            raise ValueError("Invalid SQLite header")
    uri = f"{candidate.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
        check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if check.lower() != "ok":
            raise ValueError(f"SQLite quick_check failed: {check}")
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = sorted(set(required_tables) - tables)
        if missing:
            raise ValueError(f"SQLite schema missing tables: {', '.join(missing)}")
        row_counts: Dict[str, int] = {}
        for table in nonempty_tables:
            if table not in tables:
                raise ValueError(f"SQLite schema missing table: {table}")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                raise ValueError(f"Unsafe SQLite table name: {table}")
            count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            if count <= 0:
                raise ValueError(f"SQLite table is empty: {table}")
            row_counts[table] = count
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return {
        "size": candidate.stat().st_size,
        "sha256": sha256_file(candidate),
        "quick_check": check,
        "user_version": user_version,
        "tables": sorted(tables),
        "row_counts": row_counts,
    }


def _safe_reason(reason: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(reason or "backup")).strip("-.")
    return clean[:48] or "backup"


def _display_target(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return Path(path).name


def create_backup_manifest(
    paths: Iterable[Path],
    *,
    reason: str,
    backup_root: Optional[Path] = None,
) -> Dict[str, Any]:
    root = Path(backup_root or os.getenv("INVEST_BACKUP_DIR") or DEFAULT_BACKUP_ROOT)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_id = f"{stamp}-{_safe_reason(reason)}-{uuid.uuid4().hex[:8]}"
    directory = root / backup_id
    directory.mkdir(parents=True, exist_ok=False)
    entries = []
    for source in (Path(item) for item in paths):
        if not source.exists():
            continue
        destination = directory / source.name
        if source.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            sqlite_online_backup(source, destination)
            details = inspect_sqlite(destination)
            schema_version = details["user_version"]
        else:
            shutil.copy2(source, destination)
            schema_version = None
        entries.append(
            {
                "target": _display_target(source),
                "backup_file": destination.name,
                "size": destination.stat().st_size,
                "sha256": sha256_file(destination),
                "sqlite_user_version": schema_version,
            }
        )
    manifest = {
        "backup_id": backup_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": str(reason),
        "entries": entries,
    }
    atomic_write_json(directory / "manifest.json", manifest)
    return {**manifest, "directory": str(directory)}


def backup_and_atomic_write_json(
    target: Path,
    payload: Any,
    *,
    reason: str,
    backup_root: Optional[Path] = None,
) -> Dict[str, Any]:
    backup = create_backup_manifest([target], reason=reason, backup_root=backup_root)
    atomic_write_json(target, payload)
    return backup


def backup_and_atomic_write_text(
    target: Path,
    content: str,
    *,
    reason: str,
    backup_root: Optional[Path] = None,
) -> Dict[str, Any]:
    backup = create_backup_manifest([target], reason=reason, backup_root=backup_root)
    atomic_write_text(target, content)
    return backup


def restore_sqlite_bytes(
    target: Path,
    payload: bytes,
    *,
    reason: str,
    required_tables: Sequence[str],
    nonempty_tables: Sequence[str] = (),
    force: bool = False,
    backup_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Validate an upload, back up the old DB, then atomically replace it."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    candidate = target.with_name(f".{target.name}.{uuid.uuid4().hex}.incoming")
    atomic_write_bytes(candidate, payload)
    try:
        inspection = inspect_sqlite(
            candidate,
            required_tables=required_tables,
            nonempty_tables=() if force else nonempty_tables,
        )
        backup = create_backup_manifest([target], reason=reason, backup_root=backup_root)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{target}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
        os.replace(candidate, target)
        try:
            final = inspect_sqlite(
                target,
                required_tables=required_tables,
                nonempty_tables=() if force else nonempty_tables,
            )
        except Exception:
            backup_file = Path(backup["directory"]) / target.name
            if backup_file.exists():
                rollback = target.with_name(f".{target.name}.{uuid.uuid4().hex}.rollback")
                shutil.copy2(backup_file, rollback)
                os.replace(rollback, target)
            raise
        return {
            "backup_id": backup["backup_id"],
            "incoming": inspection,
            "restored": final,
        }
    finally:
        if candidate.exists():
            candidate.unlink()


def create_sqlite_export(
    source: Path,
    *,
    required_tables: Sequence[str] = (),
    export_root: Optional[Path] = None,
) -> Path:
    root = Path(export_root or DEFAULT_BACKUP_ROOT / "exports")
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{Path(source).stem}-{uuid.uuid4().hex}.sqlite"
    sqlite_online_backup(Path(source), destination)
    inspect_sqlite(destination, required_tables=required_tables)
    return destination
