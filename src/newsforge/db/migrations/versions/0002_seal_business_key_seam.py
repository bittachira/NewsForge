"""seal business-key FK seam, PG-safe widths and numeric types

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-10 23:00:00.000000

Legacy databases were created by ``create_all`` from the pre-0001 models. That
schema stored *business keys* inside foreign-key columns declared against the
UUID primary keys (``story_signals.story_id -> stories.id``,
``publications.story_id -> stories.id``, ``source_items.source_id -> sources.id``)
and used ``String(36)``/``BigInteger`` for values that actually carry 128-char
business keys and floats — a seam that only worked because SQLite foreign keys
are OFF by default and the test data used PK==business-key. PostgreSQL ALWAYS
enforces foreign keys, so this revision re-points the three foreign keys at the
unique business-key columns (``stories.story_id``, ``sources.source_id``) and
widen/migrate the affected columns, keeping every value already stored intact.

Two execution shapes:
  * SQLite: batch_alter_table rebuilds the three seam tables from an explicit
    model (legacy SQLite FKs are UNNAMED, so name-based drops are impossible);
    the remaining alters use batch recreate too.
  * PostgreSQL: ALTER TABLE in place. A pre-0001 PostgreSQL database never
    existed in this codebase (PostgreSQL only arrives with migrations), so the
    standard ``_fkey`` constraint names are used for symmetry; adjust the names
    if a genuinely foreign legacy PG schema is ever rebased.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


# --------------------------------------------------------------------------- #
# SQLite branch — name-independent rebuild of the three seam tables
# --------------------------------------------------------------------------- #
def _story_signals_model() -> sa.Table:
    return sa.Table(
        "story_signals",
        sa.MetaData(),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("story_id", sa.String(length=128), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["source_items.id"], name="fk_story_signals_item_id_source_items"),
        sa.ForeignKeyConstraint(["story_id"], ["stories.story_id"], name="fk_story_signals_story_id_stories"),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_story_signals_story_id", "story_id"),
        sa.Index("ix_story_signals_item_id", "item_id"),
        sa.Index("ix_story_signals_created_at", "created_at"),
        sa.Index("ix_story_signals_id", "id"),
    )


def _publications_model() -> sa.Table:
    return sa.Table(
        "publications",
        sa.MetaData(),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("story_id", sa.String(length=128), nullable=False),
        sa.Column("destination_key", sa.String(length=128), nullable=False),
        sa.Column("decision_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=192), nullable=False),
        sa.Column("published_at", sa.String(length=27), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.Column("updated_at", sa.String(length=27), nullable=False),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.id"], name="fk_publications_decision_id_decisions"),
        sa.ForeignKeyConstraint(["story_id"], ["stories.story_id"], name="fk_publications_story_id_stories"),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_publications_idempotency_key", "idempotency_key", unique=True),
        sa.Index("ix_publications_story_id", "story_id"),
        sa.Index("ix_publications_destination_key", "destination_key"),
        sa.Index("ix_publications_decision_id", "decision_id"),
        sa.Index("ix_publications_published_at", "published_at"),
        sa.Index("ix_publications_created_at", "created_at"),
        sa.Index("ix_publications_updated_at", "updated_at"),
        sa.Index("ix_publications_id", "id"),
    )


def _source_items_model() -> sa.Table:
    return sa.Table(
        "source_items",
        sa.MetaData(),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("content_html", sa.Text(), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("published_at", sa.String(length=27), nullable=True),
        sa.Column("fetched_at", sa.String(length=27), nullable=False),
        sa.Column("dedupe_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["sources.source_id"], name="fk_source_items_source_id_sources"),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_source_items_dedupe_hash", "dedupe_hash"),
        sa.Index("ix_source_items_fetched_at", "fetched_at"),
        sa.Index("ix_source_items_id", "id"),
        sa.Index("ix_source_items_published_at", "published_at"),
        sa.Index("ix_source_items_source_id", "source_id"),
    )


def _rebuild_with_model(table: str, model: sa.Table) -> None:
    """Rebuild ``table`` from ``model`` preserving all data (SQLite only).

    The explicit no-op ``alter_column`` is what forces the batch recreation to
    actually run: with zero operations Alembic would emit no DDL at all."""
    with op.batch_alter_table(table, copy_from=model) as batch_op:
        batch_op.alter_column(
            "id", type_=sa.String(length=36), existing_type=sa.String(length=36)
        )


def _widen_columns_sqlite() -> None:
    # NOT NULL business-key columns (decisions/trust/quality target_id).
    for table in ("decisions", "trust_evaluations", "quality_evaluations"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column("target_id", type_=sa.String(length=128),
                                  existing_nullable=False, existing_type=sa.String(length=36))
    # Nullable entity_id columns (audit_logs / analytics).
    for table in ("audit_logs", "analytics"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column("entity_id", type_=sa.String(length=128),
                                  existing_nullable=True, existing_type=sa.String(length=36))
    # Integer/NUMERIC business metrics that actually carry floats.
    for table, column in (
        ("prices", "value"),
        ("affiliate_links", "price"),
        ("analytics", "value"),
    ):
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(column, type_=sa.Float(),
                                  existing_nullable=False, existing_type=sa.BigInteger())


# --------------------------------------------------------------------------- #
# PostgreSQL branch — in-place ALTERs (legacy PG schema never existed here)
# --------------------------------------------------------------------------- #
def _rebase_postgres_fks() -> None:
    op.drop_constraint("story_signals_story_id_fkey", "story_signals", type_="foreignkey")
    op.create_foreign_key(None, "story_signals", "stories", ["story_id"], ["story_id"])
    op.drop_constraint("publications_story_id_fkey", "publications", type_="foreignkey")
    op.create_foreign_key(None, "publications", "stories", ["story_id"], ["story_id"])
    op.drop_constraint("source_items_source_id_fkey", "source_items", type_="foreignkey")
    op.create_foreign_key(None, "source_items", "sources", ["source_id"], ["source_id"])


def _widen_columns_postgres() -> None:
    for table, column in (
        ("decisions", "target_id"),
        ("trust_evaluations", "target_id"),
        ("quality_evaluations", "target_id"),
        ("audit_logs", "entity_id"),
        ("analytics", "entity_id"),
    ):
        op.alter_column(table, column, type_=sa.String(length=128))
    for table, column in (
        ("prices", "value"),
        ("affiliate_links", "price"),
        ("analytics", "value"),
    ):
        op.alter_column(table, column, type_=sa.Float())


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _rebuild_with_model("story_signals", _story_signals_model())
        _rebuild_with_model("publications", _publications_model())
        _rebuild_with_model("source_items", _source_items_model())
        _widen_columns_sqlite()
    else:
        _rebase_postgres_fks()
        _widen_columns_postgres()


def downgrade() -> None:
    """Reversal is intentionally not provided: business keys have been the real
    storage values all along; PG-safe types are the project's baseline from here."""
    pass