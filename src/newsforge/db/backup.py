"""Offline, consistent SQLite backup + restore (OPS_HARDENING_PERSISTENCE).

Uses the SQLite Online Backup API (``sqlite3.Connection.backup``), which copies a
*snapshot-consistent* image of the database even while other processes are writing it:
a partially-written page can never be captured because every page is copied from the
same read transaction (WAL content included). No `cp` of the file, no `VACUUM INTO`
dependency — just the standard library.

Nothing secret is ever printed: errors carry only paths/filenames.

Typical usage
-------------
    from newsforge.db.backup import backup_database, restore_database
    dest = backup_database("data/newsforge.db", "data/backups")   # one consistent copy
    restore_database(dest, "data/newsforge.db")                    # safe overwrite
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

DBSource = Union[str, Path]
DBDest = Union[str, Path]


class BackupError(RuntimeError):
    """Raised when a backup/restore cannot be completed safely."""


def resolve_db_path(db: DBSource) -> Path:
    p = Path(db).expanduser()
    return p.absolute()


def _ensure_no_transaction(con: sqlite3.Connection) -> None:
    # The backup API requires the source to not be inside a transaction.
    # With isolation_level=None the connection auto-commits, so there is usually
    # nothing to roll back — swallow "no transaction is active" either way.
    con.isolation_level = None
    try:
        con.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


def timestamped_name(prefix: str = "newsforge") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{ts}.db"


def _validate_consistent(path: Path) -> None:
    """Reopen the copy and run integrity_check — one cheap full-read consistency gate."""
    try:
        con = sqlite3.connect(str(path))
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise BackupError(f"backup failed integrity check: {path} ({exc})") from exc
    if not row or (row[0] not in ("ok", 1)):
        raise BackupError(f"backup failed integrity check: {path} -> {row!r}")


def _open_dest(db: Path) -> sqlite3.Connection:
    if db.exists() and not db.is_file():
        raise BackupError(f"destination is not a file: {db}")
    db.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(db))


def backup_database(source_db: DBSource, dest: DBDest) -> Path:
    """Create ONE consistent copy of ``source_db`` at ``dest``.

    - ``dest`` an existing directory OR any path without a ``.db`` suffix -> treated as a
      directory and a timestamped copy is written inside (``newsforge-YYYYMMDD-HHMMSS.db``).
    - ``dest`` a ``.db`` file path -> written verbatim (created/overwritten).
    Raises :class:`BackupError` on any failure. Returns the resulting path.
    """
    src_path = resolve_db_path(source_db)
    if not src_path.is_file():
        raise BackupError(f"source database not found: {src_path}")

    dest_path = Path(dest).expanduser()
    if dest_path.is_dir() or dest_path.suffix.lower() != ".db":
        dest_path = dest_path / timestamped_name()

    try:
        src = sqlite3.connect(str(src_path))
        try:
            _ensure_no_transaction(src)
            dst = _open_dest(dest_path)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as exc:
        raise BackupError(f"backup of {src_path} failed: {exc}") from exc

    _validate_consistent(dest_path)
    return dest_path


def restore_database(backup_file: DBSource, dest_db: DBDest) -> Path:
    """Restore a backup over ``dest_db`` using the same snapshot-consistent copy API.

    The destination is overwritten atomically at the connection level (the backup API
    copies page-by-page inside one transaction). Returns the destination path.
    """
    backup_path = resolve_db_path(backup_file)
    if not backup_path.is_file():
        raise BackupError(f"backup file not found: {backup_path}")

    dest_path = Path(dest_db).expanduser()
    try:
        src = sqlite3.connect(str(backup_path))
        try:
            _ensure_no_transaction(src)
            dst = _open_dest(dest_path)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as exc:
        raise BackupError(f"restore of {backup_path} into {dest_path} failed: {exc}") from exc

    _validate_consistent(dest_path)
    return dest_path