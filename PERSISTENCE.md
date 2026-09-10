# PERSISTENCE (OPS_HARDENING_PERSISTENCE)

## 1. Single source of truth: `/data`

`/data` is the ONLY persistent application data directory (container). It is mounted
from a Docker volume; nothing else in the image must be treated as durable.

| Path                          | Contents                                   | Needs backup | Persists across restarts | Recreatable |
|-------------------------------|--------------------------------------------|--------------|--------------------------|-------------|
| `/data/newsforge.db`          | The SQLite database (all NewsForge state)  | **Yes**      | Yes                      | No          |
| `/data/newsforge.db-wal`      | SQLite WAL sidecar (active transactions)   | Yes (with DB)| Yes                      | Yes*        |
| `/data/newsforge.db-shm`      | SQLite shared-memory index (WAL)           | No           | Yes                      | Yes*        |
| `/data/backups/newsforge-*.db`| Offline SQLite backups                     | **Yes**      | Yes                      | No          |

`-wal` / `-shm` are auto-managed by SQLite in WAL mode: if they exist next to the DB
they belong to `/data/newsforge.db`. `*` They are safely recreated/trimmed by SQLite on
next open; **do not** delete them manually while the app is running. Backup via the
SQLite Online Backup API (below) captures the WAL content consistently, so a backup is
valid even if taken live.

## 2. The database

- Single SQLite file; default `data/newsforge.db` (resolved against the source root at
  runtime). Override with `NEWSFORGE_DB_PATH`.
- All models live under `src/newsforge/db/models.py`; the ORM stays portable
  (String/Text/BigInteger, JSON stored as text via `to_jsonable`/`from_jsonable`,
  timestamps as ISO-8601 strings).
- SQLite tuned via per-connection PRAGMAs (`build_engine`):
  - `journal_mode=WAL`
  - `synchronous=NORMAL` (crash-safe + fast)
  - `busy_timeout=30000`
  - `foreign_keys=OFF` — **deliberate**: the MVP stores business keys inside FK columns
    (`story_signals.story_id`, `publications.story_id` → `stories.id`). Enabling FK
    enforcement today would reject legitimate inserts; fix the seam in a migration first.

## 3. Backup (offline, consistent)

```bash
python - <<'PY'
from newsforge.config import DatabaseConfig
from newsforge.db.backup import backup_database
cfg = DatabaseConfig()
backup_database(cfg.path, cfg.backup_dir)   # -> newsforge-YYYYMMDD-HHMMSS.db
PY
```

- Uses the SQLite Online Backup API: a snapshot-consistent copy, safe even while the
  app is writing (no partially written pages, no `cp`).
- Destination may be a directory (timestamped file) or an explicit file path.
- Each backup is verified with `PRAGMA integrity_check` before returning.
- **Restore**: `restore_database(<backup>, /data/newsforge.db)` overwrites the DB
  consistently; reopen the app to reload. Demonstrate the cycle any time:
  `backup → restore → open DB → read expected data` (covered by `tests/test_persistence.py`).

## 4. What must persist vs what can be recreated

- **Must persist**: `/data/newsforge.db` (+ its WAL) and any completed backups.
- **Recreable**: the schema (auto-created on startup), the virtualenv, app code
  (rebuilt image), `.env`? (no — env is injected, not stored in image).

## 5. Schema boundary (no Alembic yet)

- `init_db` runs `create_all` (creates missing tables only, never alters existing ones),
  then `newsforge.db.schema.ensure_schema_compatible`:
  - stamps `_newsforge_meta.schema_version` (currently `1`) on first boot (adopts
    pre-existing DBs),
  - **refuses to start** if the stored version is newer (downgrade) or older (migration
    required) than the build,
  - **refuses to start** if an existing table is missing a declared column (fail-fast
    instead of runtime `no such column`).
- Bumping `SCHEMA_VERSION` is reserved for the migration phase; do it only as part of a
  real migration.

## 6. PostgreSQL readiness — status: PARTIAL

Portable today: ORM-typed columns, JSON-as-text helpers, ISO timestamps, all queries
ORM-generated (only raw SQL is `SELECT 1` in the health probe). Blockers/gaps:

1. `story_signals.story_id` and `publications.story_id` declare FK → `stories.id` but
   store business-key values; PostgreSQL enforces FKs by default → referential fix needed.
2. No migration tooling yet (the schema version boundary above is the checkpoint).
3. Native JSON/timestamps are optional niceties, not blockers.
4. `NEWSFORGE_DB_PATH=<dsn>` already routes to the PostgreSQL dialect (removed the Path
   coercion bug) — see `build_engine`.