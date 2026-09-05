"""Database layer: declarative base, models, session factory."""
from newsforge.db.base import Base  # noqa: F401
from newsforge.db.models import (  # noqa: F401
    users, authors, sources, source_items, stories, story_signals, articles, claims, fact_checks,
    entities, entity_relationships, products, prices, laws_regulations, tools,
    newsletters, subscriptions, affiliate_links, affiliate_clicks, ad_slots,
    analytics, ai_jobs, ai_runs, experiments, notifications, content_scores,
    audit_logs, errors,
)
from newsforge.db.session import (  # noqa: F401
    build_engine, get_session_factory, set_session_factory, switch_default_database,
    use_isolated_database, use_isolated_database_ctx, get_session, init_db,
)

__all__ = [
    "Base",
    "users", "authors", "sources", "source_items", "stories", "story_signals",
    "claims", "fact_checks", "entities", "entity_relationships", "products",
    "prices", "laws_regulations", "tools", "newsletters", "subscriptions",
    "affiliate_links", "affiliate_clicks", "ad_slots", "analytics", "ai_jobs",
    "ai_runs", "experiments", "notifications", "content_scores", "audit_logs",
    "errors",
    "build_engine", "get_session_factory", "set_session_factory",
    "switch_default_database", "use_isolated_database", "use_isolated_database_ctx",
    "get_session", "init_db",
]
