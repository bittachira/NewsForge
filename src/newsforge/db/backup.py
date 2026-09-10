"""Offline, consistent database backup + restore (OPS_HARDENING_PERSISTENCE).

SQLite uses the Online Backup API (``sqlite3.Connection.backup``), which copies a
*snapshot-consistent* image even while other processes write it (WAL included).
PostgreSQL uses ``pg_dump`` in custom format, verified with ``pg_restore --list``.
Both providers share :class:`DatabaseBackupProvider`; the factory selects the
right one from the active :class:`~newsforge.config.DatabaseConfig`.

Nothing secret is ever printed: errors carry only paths/filenames and — for
PostgreSQL, where the CLI tools echo the DSN — a *redacted* DSN (password masked).

Typical usage
-------------
    from newsforge.db.backup import backup_database, restore_database
    dest = backup_database("data/newsforge.db", "data/backups")   # one consistent copy
    restore_database(dest, "data/newsforge.db")                    # safe overwrite

    from newsforge.db.backup import get_backup_provider
    provider = get_backup_provider()        # SQLite or PostgreSQL from the config
    snapshot = provider.create_backup(cfg.backup_dir)
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Union

from newsforge.config import DatabaseConfig, redact_dsn

DBSource = Union[str, Path]
DBDest = Union[str, Path]


class BackupError(RuntimeError):
    """Raised when a backup/restore cannot be completed safely."""


class DatabaseBackupProvider(Protocol):
    """Backup strategy for a live database (one dialect)."""

    def create_backup(self, dest: DBDest) -> Path:
        """Create one consistent snapshot at ``dest`` (dir or file); return its path."""
        ...

    def verify(self, backup_path: DBSource) -> None:
        """Cheap consistency gate over an existing backup; raise :class:`BackupError`."""
        ...


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


# --------------------------------------------------------------------------- #
# Provider abstraction (SQLite/PostgreSQL) + factory
# --------------------------------------------------------------------------- #
@dataclass
class SQLiteBackupProvider:
    """Consistent file-level snapshots via the SQLite Online Backup API."""

    db_path: DBSource

    def create_backup(self, dest: DBDest) -> Path:
        return backup_database(self.db_path, dest)

    def verify(self, backup_path: DBSource) -> None:
        _validate_consistent(resolve_db_path(backup_path))


@dataclass
class PostgresBackupProvider:
    """``pg_dump`` custom-format archives, verified with ``pg_restore --list``.

    Requires the PostgreSQL client tools (``pg_dump`` / ``pg_restore``) on PATH —
    the CI PostgreSQL service container ships them. Authentication follows the
    DSN/``PG*`` environment conventions; the DSN is only ever surfaced redacted."""

    dsn: str

    def _dest_path(self, dest: DBDest) -> Path:
        p = Path(dest).expanduser()
        if p.is_dir() or p.suffix.lower() not in (".dump", ".pgdump", ".backup"):
            p = p / timestamped_name(prefix="newsforge").replace(".db", ".dump")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def create_backup(self, dest: DBDest) -> Path:
        out = self._dest_path(dest)
        cmd = [
            "pg_dump",
            "--format=custom",
            "--compress=9",
            "--no-owner",
            "--no-privileges",
            "--file", str(out),
            self.dsn,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
        except OSError as exc:  # pg_dump not installed / not on PATH
            raise BackupError(
                f"pg_dump unavailable (dsn={redact_dsn(self.dsn)}): {exc}"
            ) from exc
        if proc.returncode != 0:
            hint = str(proc.stderr or proc.stdout or "").strip()
            raise BackupError(
                f"pg_dump failed (dsn={redact_dsn(self.dsn)}): {hint or 'exit ' + str(proc.returncode)}"
            )
        self.verify(out)
        return out

    def verify(self, backup_path: DBSource) -> None:
        p = Path(backup_path).expanduser()
        if not p.is_file():
            raise BackupError(f"backup file not found: {p}")
        try:
            proc = subprocess.run(  # noqa: S603
                ["pg_restore", "--list", str(p)], capture_output=True, text=True
            )
        except OSError as exc:
            raise BackupError(f"pg_restore unavailable for {p}: {exc}") from exc
        if proc.returncode != 0:
            hint = str(proc.stderr or proc.stdout or "").strip()[:200]
            raise BackupError(f"pg_restore listed {p} as invalid: {hint or 'exit ' + str(proc.returncode)}")


def get_backup_provider(cfg: DatabaseConfig | None = None) -> DatabaseBackupProvider:
    """Return the provider matching the active configuration's dialect.

    PostgreSQL DSNs (``postgresql://…``) select :class:`PostgresBackupProvider`;
    everything else (the default SQLite path) selects :class:`SQLiteBackupProvider`."""
    cfg = cfg or DatabaseConfig()
    raw = str(cfg.path)
    if raw.startswith(("postgresql", "postgres")):
        return PostgresBackupProvider(dsn=raw)
    return SQLiteBackupProvider(db_path=cfg.path)