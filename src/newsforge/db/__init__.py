"""Database layer: declarative base, models, session factory."""
from newsforge.db.base import Base  # noqa: F401
from newsforge.db.models import (  # noqa: F401
    users, authors, sources, source_items, stories, story_signals, articles, claims, fact_checks,
    claim_evidence, trust_evaluations, quality_evaluations, decisions, review_tasks,
    entities, entity_relationships, products, prices, laws_regulations, tools,
    newsletters, subscriptions, affiliate_links, affiliate_clicks, ad_slots,
    analytics, ai_jobs, ai_runs, experiments, notifications, content_scores,
    audit_logs, errors, publications, publication_attempts, PublicationStatus, DestinationType,
    publication_metrics, destination_metrics, published_snapshots, postpublish_events,
    generated_artifacts, ArtifactFormat, GenerationState,
)
from newsforge.db.session import (  # noqa: F401
    build_engine, get_session_factory, set_session_factory, switch_default_database,
    use_isolated_database, use_isolated_database_ctx, get_session, init_db,
    init_production_db, migrate_database, migration_head_revision,
    on_disk_migration_revision, migration_state, assert_schema_migrated,
    MigrationIncompatibilityError,
)
from newsforge.db.backup import (  # noqa: F401
    backup_database, restore_database, BackupError,
    get_backup_provider, SQLiteBackupProvider, PostgresBackupProvider,
    DatabaseBackupProvider,
)
from newsforge.db.schema import (  # noqa: F401
    SCHEMA_VERSION, SchemaIncompatibleError, ensure_schema_compatible,
    ensure_schema_version, validate_column_drift,
)

__all__ = [
    "Base",
    "users", "authors", "sources", "source_items", "stories", "story_signals",
    "articles", "claims", "fact_checks", "claim_evidence", "trust_evaluations",
    "quality_evaluations", "decisions", "review_tasks", "entities",
    "entity_relationships", "products", "prices", "laws_regulations", "tools",
    "newsletters", "subscriptions", "affiliate_links", "affiliate_clicks", "ad_slots",
    "analytics", "ai_jobs", "ai_runs", "experiments", "notifications", "content_scores",
    "audit_logs", "errors", "publications", "publication_attempts",
    "PublicationStatus", "DestinationType",
    # Measurement + post-publish observability (section 27) -- new in P5
    "publication_metrics", "destination_metrics", "published_snapshots", "postpublish_events",
    # Generated editorial artifacts (P6) -- deterministic, evidence-bound generation
    "generated_artifacts", "ArtifactFormat", "GenerationState",
    "build_engine", "get_session_factory", "set_session_factory",
    "switch_default_database", "use_isolated_database", "use_isolated_database_ctx",
    "get_session", "init_db", "init_production_db", "migrate_database",
    "migration_head_revision", "on_disk_migration_revision", "migration_state",
    "assert_schema_migrated", "MigrationIncompatibilityError",
    # Persistence (OPS hardening): consistent offline backup/restore + schema boundary
    "backup_database", "restore_database", "BackupError",
    "get_backup_provider", "SQLiteBackupProvider", "PostgresBackupProvider",
    "DatabaseBackupProvider",
    "SCHEMA_VERSION", "SchemaIncompatibleError", "ensure_schema_compatible",
    "ensure_schema_version", "validate_column_drift",
]
